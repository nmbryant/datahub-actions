import logging
import traceback
from typing import Any, Dict, List, Optional

from datahub.configuration import ConfigModel
from datahub.graph.client import DatahubClientConfig, DataHubGraph

from datahub_actions.action.action import Action
from datahub_actions.action.action_registry import action_registry
from datahub_actions.api.action_core import AcrylDataHubGraph
from datahub_actions.pipeline.context import ActionContext
from datahub_actions.source.event_source import EventSource
from datahub_actions.source.event_source_registry import event_source_registry
from datahub_actions.transform.event_transformer import Transformer
from datahub_actions.transform.event_transformer_registry import (
    event_transformer_registry,
)
from datahub_actions.transform.filter.filter_transformer import (
    FilterTransformer,
    FilterTransformerConfig,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class SourceConfig(ConfigModel):
    type: str
    config: Optional[Dict[str, Any]]


class TransformConfig(ConfigModel):
    type: str
    config: Optional[Dict[str, Any]]


class FilterConfig(ConfigModel):
    event_type: str
    fields: Dict[str, Any]


class ActionConfig(ConfigModel):
    type: str
    config: Optional[dict]


class PipelineConfig(ConfigModel):
    name: str
    source: SourceConfig
    filter: Optional[FilterConfig]
    transform: Optional[List[TransformConfig]]
    action: ActionConfig
    datahub: DatahubClientConfig


def create_action_context(datahub_config: DatahubClientConfig) -> ActionContext:
    return ActionContext(AcrylDataHubGraph(DataHubGraph(datahub_config)))


def create_event_source(source_config: SourceConfig, ctx: ActionContext) -> EventSource:
    event_source_type = source_config.type
    event_source_class = event_source_registry.get(event_source_type)
    try:
        logger.debug(
            f"Attempting to instantiate new Event Source of type {source_config.type}.."
        )
        event_source_config = (
            source_config.config if source_config.config is not None else {}
        )
        return event_source_class.create(event_source_config, ctx)
    except Exception as e:
        logger.error(
            f"Caught exception while attempting to instantiate Event Source of type {source_config.type}: {traceback.format_exc(limit=3)}"
        )
        raise Exception(
            f"Caught exception while attempting to instantiate Event Source of type {source_config.type}"
        ) from e


def create_filter_transformer(
    filter_config: FilterConfig, ctx: ActionContext
) -> Transformer:
    try:
        logger.debug("Attempting to instantiate filter transformer..")
        filter_transformer_config = FilterTransformerConfig(
            event_type=filter_config.event_type, fields=filter_config.fields
        )
        return FilterTransformer(filter_transformer_config)
    except Exception as e:
        logger.error(
            f"Caught exception while attempting to instantiate Filter transformer: {traceback.format_exc(limit=3)}"
        )
        raise Exception(
            "Caught exception while attempting to instantiate Filter transformer"
        ) from e


def create_transformer(
    transform_config: TransformConfig, ctx: ActionContext
) -> Transformer:
    transformer_type = transform_config.type
    transformer_class = event_transformer_registry.get(transformer_type)
    try:
        logger.debug(
            f"Attempting to instantiate new Transformer of type {transform_config.type}.."
        )
        transformer_config = (
            transform_config.config if transform_config.config is not None else {}
        )
        return transformer_class.create(transformer_config, ctx)
    except Exception as e:
        logger.error(
            f"Caught exception while attempting to instantiate Transformer: {traceback.format_exc(limit=3)}"
        )
        raise Exception(
            "Caught exception while attempting to instantiate Transformer"
        ) from e


def create_action(action_config: ActionConfig, ctx: ActionContext) -> Action:
    action_type = action_config.type
    action_class = action_registry.get(action_type)
    try:
        logger.debug(
            f"Attempting to instantiate new Action of type {action_config.type}.."
        )
        action_config_dict = (
            action_config.config if action_config.config is not None else {}
        )
        return action_class.create(action_config_dict, ctx)
    except Exception as e:
        logger.error(
            f"Caught exception while attempting to instantiate Action: {traceback.format_exc(limit=3)}"
        )
        raise Exception(
            "Caught exception while attempting to instantiate Action"
        ) from e


# A component responsible for executing a single Actions pipeline.
class Pipeline:
    name: str
    source: EventSource
    transforms: List[Transformer] = []
    action: Action

    shutdown: bool = False

    def __init__(
        self,
        name: str,
        source: EventSource,
        transforms: List[Transformer],
        action: Action,
    ) -> None:
        self.name = name
        self.source = source
        self.transforms = transforms
        self.action = action

    @classmethod
    def create(cls, config_dict: dict) -> "Pipeline":
        config = PipelineConfig.parse_obj(config_dict)

        # Create Context
        ctx = create_action_context(config.datahub)

        # Create Event Source
        event_source = create_event_source(config.source, ctx)

        # Create Transforms
        transforms = []
        if config.filter is not None:
            transforms.append(create_filter_transformer(config.filter, ctx))

        if config.transform is not None:
            for transform_config in config.transform:
                transforms.append(create_transformer(transform_config, ctx))

        # Create Action
        action = create_action(config.action, ctx)

        # Finally, create Pipeline.
        return cls(config.name, event_source, transforms, action)

    # Launch the Pipeline.
    def start(self):
        enveloped_events = self.source.events()

        for enveloped_event in enveloped_events:
            if self.shutdown is True:
                self.source.close()
                logger.info(f"Stopping Actions Pipeline with name {self.name}")
                return

            # First, invoke transformers
            curr_event = enveloped_event
            for transformer in self.transforms:
                transformed_event = transformer.transform(curr_event)
                if curr_event is None:
                    # Short circuit event. Skip to ack phase.
                    self.source.ack(enveloped_event)
                    continue
                else:
                    curr_event = transformed_event  # type: ignore

            # Finally, invoke the action
            self.action.act(curr_event)

            # Finally, ack the event.
            self.source.ack(enveloped_event)

    # Terminate the pipeline.
    def stop(self):
        logger.info(f"Preparing to stop Actions Pipeline with name {self.name}")
        self.shutdown = True
