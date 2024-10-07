from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from celery import Task, current_task
from django.urls import reverse
from requests.exceptions import RequestException

from sentry import analytics
from sentry.api.serializers import serialize
from sentry.constants import SentryAppInstallationStatus
from sentry.db.models.base import Model
from sentry.eventstore.models import Event, GroupEvent
from sentry.hybridcloud.rpc.caching import region_caching_service
from sentry.models.activity import Activity
from sentry.models.group import Group
from sentry.models.organization import Organization
from sentry.models.organizationmapping import OrganizationMapping
from sentry.models.project import Project
from sentry.sentry_apps.api.serializers.app_platform_event import AppPlatformEvent
from sentry.sentry_apps.models.sentry_app import VALID_EVENTS, SentryApp
from sentry.sentry_apps.models.sentry_app_installation import SentryAppInstallation
from sentry.sentry_apps.models.servicehook import ServiceHook, ServiceHookProject
from sentry.sentry_apps.services.app.model import RpcSentryAppInstallation
from sentry.sentry_apps.services.app.service import (
    app_service,
    get_by_application_id,
    get_installation,
)
from sentry.shared_integrations.exceptions import ApiHostError, ApiTimeoutError, ClientError
from sentry.silo.base import SiloMode
from sentry.tasks.base import instrumented_task, retry
from sentry.users.services.user.model import RpcUser
from sentry.users.services.user.service import user_service
from sentry.utils import metrics
from sentry.utils.http import absolute_uri
from sentry.utils.sentry_apps import send_and_save_webhook_request
from sentry.utils.sentry_apps.service_hook_manager import (
    create_or_update_service_hooks_for_installation,
)

logger = logging.getLogger("sentry.sentry_apps.tasks.sentry_apps")

TASK_OPTIONS = {
    "queue": "app_platform",
    "default_retry_delay": (60 * 5),  # Five minutes.
    "max_retries": 3,
    "record_timing": True,
    "silo_mode": SiloMode.REGION,
}
CONTROL_TASK_OPTIONS = {
    "queue": "app_platform.control",
    "default_retry_delay": (60 * 5),  # Five minutes.
    "max_retries": 3,
    "silo_mode": SiloMode.CONTROL,
}

retry_decorator = retry(
    on=(RequestException, ApiHostError, ApiTimeoutError),
    ignore=(ClientError,),
)

# We call some models by a different name, publicly, than their class name.
# For example the model Group is called "Issue" in the UI. We want the Service
# Hook events to match what we externally call these primitives.
RESOURCE_RENAMES = {"Group": "issue"}

TYPES = {"Group": Group, "Error": Event, "Comment": Activity}


def _webhook_event_data(
    event: Event | GroupEvent, group_id: int, project_id: int
) -> dict[str, Any]:
    project = Project.objects.get_from_cache(id=project_id)
    organization = Organization.objects.get_from_cache(id=project.organization_id)

    event_context = event.as_dict()
    event_context["url"] = absolute_uri(
        reverse(
            "sentry-api-0-project-event-details",
            args=[project.organization.slug, project.slug, event.event_id],
        )
    )

    event_context["web_url"] = absolute_uri(
        reverse(
            "sentry-organization-event-detail", args=[organization.slug, group_id, event.event_id]
        )
    )

    # The URL has a regex OR in it ("|") which means `reverse` cannot generate
    # a valid URL (it can't know which option to pick). We have to manually
    # create this URL for, that reason.
    event_context["issue_url"] = absolute_uri(f"/api/0/issues/{group_id}/")
    event_context["issue_id"] = str(group_id)
    return event_context


@instrumented_task(name="sentry.sentry_apps.tasks.sentry_apps.send_alert_event", **TASK_OPTIONS)
@retry_decorator
def send_alert_event(
    event: Event | GroupEvent,
    rule: str,
    sentry_app_id: int,
    additional_payload_key: str | None = None,
    additional_payload: Mapping[str, Any] | None = None,
) -> None:
    """
    When an incident alert is triggered, send incident data to the SentryApp's webhook.
    :param event: The `Event` for which to build a payload.
    :param rule: The AlertRule that was triggered.
    :param sentry_app_id: The SentryApp to notify.
    :param additional_payload_key: The key used to attach additional data to the webhook payload
    :param additional_payload: The extra data attached to the payload body at the key specified by `additional_payload_key`.
    :return:
    """
    group = event.group
    assert group, "Group must exist to get related attributes"
    project = Project.objects.get_from_cache(id=group.project_id)
    organization = Organization.objects.get_from_cache(id=project.organization_id)

    extra = {
        "sentry_app_id": sentry_app_id,
        "project_slug": project.slug,
        "organization_slug": organization.slug,
        "rule": rule,
    }

    sentry_app = app_service.get_sentry_app_by_id(id=sentry_app_id)
    if sentry_app is None:
        logger.info("event_alert_webhook.missing_sentry_app", extra=extra)
        return

    installations = app_service.get_many(
        filter=dict(
            organization_id=organization.id,
            app_ids=[sentry_app.id],
            status=SentryAppInstallationStatus.INSTALLED,
        )
    )
    if not installations:
        logger.info("event_alert_webhook.missing_installation", extra=extra)
        return
    (install,) = installations

    event_context = _webhook_event_data(event, group.id, project.id)

    data = {"event": event_context, "triggered_rule": rule}

    # Attach extra payload to the webhook
    if additional_payload_key and additional_payload:
        data[additional_payload_key] = additional_payload

    request_data = AppPlatformEvent(
        resource="event_alert", action="triggered", install=install, data=data
    )

    send_and_save_webhook_request(sentry_app, request_data)

    # On success, record analytic event for Alert Rule UI Component
    if request_data.data.get("issue_alert"):
        analytics.record(
            "alert_rule_ui_component_webhook.sent",
            organization_id=organization.id,
            sentry_app_id=sentry_app_id,
            event=f"{request_data.resource}.{request_data.action}",
        )


def _process_resource_change(
    *,
    action: str,
    sender: str,
    instance_id: int,
    retryer: Task | None = None,
    **kwargs: Any,
) -> None:
    # The class is serialized as a string when enqueueing the class.
    model: type[Event] | type[Model] = TYPES[sender]
    instance: Event | Model | None = None
    # The Event model has different hooks for the different event types. The sender
    # determines which type eg. Error and therefore the 'name' eg. error
    if issubclass(model, Event):
        if not kwargs.get("instance"):
            extra = {"sender": sender, "action": action, "event_id": instance_id}
            logger.info("process_resource_change.event_missing_event", extra=extra)
            return
        name = sender.lower()
    else:
        # Some resources are named differently than their model. eg. Group vs Issue.
        # Looks up the human name for the model. Defaults to the model name.
        name = RESOURCE_RENAMES.get(model.__name__, model.__name__.lower())

    # By default, use Celery's `current_task` but allow a value to be passed for the
    # bound Task.
    retryer = retryer or current_task

    # We may run into a race condition where this task executes before the
    # transaction that creates the Group has committed.
    if issubclass(model, Event):
        # XXX:(Meredith): Passing through the entire event was an intentional choice
        # to avoid having to query NodeStore again for data we had previously in
        # post_process. While this is not ideal, changing this will most likely involve
        # an overhaul of how we do things in post_process, not just this task alone.
        instance = kwargs.get("instance")
    else:
        try:
            instance = model.objects.get(id=instance_id)
        except model.DoesNotExist as e:
            # Explicitly requeue the task, so we don't report this to Sentry until
            # we hit the max number of retries.
            return retryer.retry(exc=e)

    event = f"{name}.{action}"

    if event not in VALID_EVENTS:
        return

    org = None

    if isinstance(instance, (Group, Event, GroupEvent)):
        org = Organization.objects.get_from_cache(
            id=Project.objects.get_from_cache(id=instance.project_id).organization_id
        )
        assert org, "organization must exist to get related sentry app installations"
        installations: list[RpcSentryAppInstallation] = [
            installation
            for installation in app_service.get_installed_for_organization(organization_id=org.id)
            if event in installation.sentry_app.events
        ]

        for installation in installations:
            data = {}
            if isinstance(instance, (Event, GroupEvent)):
                assert instance.group_id, "group id is required to create webhook event data"
                data[name] = _webhook_event_data(instance, instance.group_id, instance.project_id)
            else:
                data[name] = serialize(instance)

            # Trigger a new task for each webhook
            send_resource_change_webhook.delay(
                installation_id=installation.id, event=event, data=data
            )


@instrumented_task(
    "sentry.sentry_apps.tasks.sentry_apps.process_resource_change_bound", bind=True, **TASK_OPTIONS
)
@retry_decorator
def process_resource_change_bound(
    self: Task, action: str, sender: str, instance_id: int, **kwargs: Any
) -> None:
    _process_resource_change(
        action=action, sender=sender, instance_id=instance_id, retryer=self, **kwargs
    )


@instrumented_task(
    name="sentry.sentry_apps.tasks.sentry_apps.installation_webhook", **CONTROL_TASK_OPTIONS
)
@retry_decorator
def installation_webhook(installation_id: int, user_id: int, *args: Any, **kwargs: Any) -> None:
    from sentry.mediators.sentry_app_installations.installation_notifier import InstallationNotifier

    extra = {"installation_id": installation_id, "user_id": user_id}
    try:
        # we should send the webhook for pending installations on the install event in case that's part of the workflow
        install = SentryAppInstallation.objects.get(id=installation_id)
    except SentryAppInstallation.DoesNotExist:
        logger.info("installation_webhook.missing_installation", extra=extra)
        return

    user = user_service.get_user(user_id=user_id)
    if not user:
        logger.info("installation_webhook.missing_user", extra=extra)
        return

    InstallationNotifier.run(install=install, user=user, action="created")


@instrumented_task(
    name="sentry.sentry_apps.tasks.sentry_apps.clear_region_cache", **CONTROL_TASK_OPTIONS
)
def clear_region_cache(sentry_app_id: int, region_name: str) -> None:
    try:
        sentry_app = SentryApp.objects.get(id=sentry_app_id)
    except SentryApp.DoesNotExist:
        return

    # When a sentry app's definition changes purge cache for all the installations.
    # This could get slow for large applications, but generally big applications don't change often.
    install_query = SentryAppInstallation.objects.filter(
        sentry_app=sentry_app,
    ).values("id", "organization_id")

    # There isn't a constraint on org : sentryapp so we have to handle lists
    install_map: dict[int, list[int]] = defaultdict(list)
    for install_row in install_query:
        install_map[install_row["organization_id"]].append(install_row["id"])

    # Clear application_id cache
    region_caching_service.clear_key(
        key=get_by_application_id.key_from(sentry_app.application_id), region_name=region_name
    )

    # Limit our operations to the region this outbox is for.
    # This could be a single query if we use raw_sql.
    region_query = OrganizationMapping.objects.filter(
        organization_id__in=list(install_map.keys()), region_name=region_name
    ).values("organization_id")
    for region_row in region_query:
        installs = install_map[region_row["organization_id"]]
        for install_id in installs:
            region_caching_service.clear_key(
                key=get_installation.key_from(install_id), region_name=region_name
            )


@instrumented_task(
    name="sentry.sentry_apps.tasks.sentry_apps.workflow_notification", **TASK_OPTIONS
)
@retry_decorator
def workflow_notification(
    installation_id: int, issue_id: int, type: str, user_id: int, *args: Any, **kwargs: Any
) -> None:
    webhook_data = get_webhook_data(installation_id, issue_id, user_id)
    if not webhook_data:
        return
    install, issue, user = webhook_data
    data = kwargs.get("data", {})
    data.update({"issue": serialize(issue)})
    send_webhooks(installation=install, event=f"issue.{type}", data=data, actor=user)
    analytics.record(
        f"sentry_app.issue.{type}",
        user_id=user_id,
        group_id=issue_id,
        installation_id=installation_id,
    )


@instrumented_task(
    name="sentry.sentry_apps.tasks.sentry_apps.build_comment_webhook", **TASK_OPTIONS
)
@retry_decorator
def build_comment_webhook(
    installation_id: int, issue_id: int, type: str, user_id: int, *args: Any, **kwargs: Any
) -> None:
    webhook_data = get_webhook_data(installation_id, issue_id, user_id)
    if not webhook_data:
        return None
    install, _, user = webhook_data
    data = kwargs.get("data", {})
    project_slug = data.get("project_slug")
    comment_id = data.get("comment_id")
    payload = {
        "comment_id": data.get("comment_id"),
        "issue_id": issue_id,
        "project_slug": data.get("project_slug"),
        "timestamp": data.get("timestamp"),
        "comment": data.get("comment"),
    }
    send_webhooks(installation=install, event=type, data=payload, actor=user)
    # `type` is comment.created, comment.updated, or comment.deleted
    analytics.record(
        type,
        user_id=user_id,
        group_id=issue_id,
        project_slug=project_slug,
        installation_id=installation_id,
        comment_id=comment_id,
    )


def get_webhook_data(
    installation_id: int, issue_id: int, user_id: int
) -> tuple[RpcSentryAppInstallation, Group, RpcUser | None] | None:
    extra = {"installation_id": installation_id, "issue_id": issue_id}
    install = app_service.installation_by_id(id=installation_id)
    if not install:
        logger.info("workflow_notification.missing_installation", extra=extra)
        return None

    try:
        issue = Group.objects.get(id=issue_id)
    except Group.DoesNotExist:
        logger.info("workflow_notification.missing_issue", extra=extra)
        return None

    user = None
    if user_id:
        user = user_service.get_user(user_id=user_id)
        if not user:
            logger.info("workflow_notification.missing_user", extra=extra)

    return (install, issue, user)


@instrumented_task(
    "sentry.sentry_apps.tasks.sentry_apps.send_resource_change_webhook", **TASK_OPTIONS
)
@retry_decorator
def send_resource_change_webhook(
    installation_id: int, event: str, data: dict[str, Any], *args: Any, **kwargs: Any
) -> None:
    installation = app_service.installation_by_id(id=installation_id)
    if not installation:
        logger.info(
            "send_resource_change_webhook.missing_installation",
            extra={"installation_id": installation_id, "event": event},
        )
        return

    send_webhooks(installation, event, data=data)

    metrics.incr("resource_change.processed", sample_rate=1.0, tags={"change_event": event})


def notify_sentry_app(event: Event | GroupEvent, futures):
    for f in futures:
        if not f.kwargs.get("sentry_app"):
            continue

        extra_kwargs: dict[str, Any] = {
            "additional_payload_key": None,
            "additional_payload": None,
        }
        # If the future comes from a rule with a UI component form in the schema, append the issue alert payload
        settings = f.kwargs.get("schema_defined_settings")
        if settings:
            extra_kwargs["additional_payload_key"] = "issue_alert"
            extra_kwargs["additional_payload"] = {
                "id": f.rule.id,
                "title": f.rule.label,
                "sentry_app_id": f.kwargs["sentry_app"].id,
                "settings": settings,
            }

        send_alert_event.delay(
            event=event,
            rule=f.rule.label,
            sentry_app_id=f.kwargs["sentry_app"].id,
            **extra_kwargs,
        )


def send_webhooks(installation: RpcSentryAppInstallation, event: str, **kwargs: Any) -> None:
    servicehook: ServiceHook
    try:
        servicehook = ServiceHook.objects.get(
            organization_id=installation.organization_id, actor_id=installation.id
        )
    except ServiceHook.DoesNotExist:
        logger.info(
            "send_webhooks.missing_servicehook",
            extra={"installation_id": installation.id, "event": event},
        )
        return None

    if event not in servicehook.events:
        return None

    # The service hook applies to all projects if there are no
    # ServiceHookProject records. Otherwise we want check if
    # the event is within the allowed projects.
    project_limited = ServiceHookProject.objects.filter(service_hook_id=servicehook.id).exists()

    # TODO(nola): This is disabled for now, because it could potentially affect internal integrations w/ error.created
    # # If the event is error.created & the request is going out to the Org that owns the Sentry App,
    # # Make sure we don't send the request, to prevent potential infinite loops
    # if (
    #     event == "error.created"
    #     and installation.organization_id == installation.sentry_app.owner_id
    # ):
    #     # We just want to exclude error.created from the project that the integration lives in
    #     # Need to first implement project mapping for integration partners
    #     metrics.incr(
    #         "webhook_request.dropped",
    #         tags={"sentry_app": installation.sentry_app.id, "event": event},
    #     )
    #     return

    if not project_limited:
        resource, action = event.split(".")

        kwargs["resource"] = resource
        kwargs["action"] = action
        kwargs["install"] = installation

        request_data = AppPlatformEvent(**kwargs)
        send_and_save_webhook_request(
            installation.sentry_app,
            request_data,
            installation.sentry_app.webhook_url,
        )


@instrumented_task(
    "sentry.sentry_apps.tasks.sentry_apps.create_or_update_service_hooks_for_sentry_app",
    **CONTROL_TASK_OPTIONS,
)
def create_or_update_service_hooks_for_sentry_app(
    sentry_app_id: int, webhook_url: str, events: list[str], **kwargs: dict
) -> None:
    installations = SentryAppInstallation.objects.filter(sentry_app_id=sentry_app_id)
    for installation in installations:
        create_or_update_service_hooks_for_installation(
            installation=installation,
            events=events,
            webhook_url=webhook_url,
        )