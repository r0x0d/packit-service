# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

from typing import Tuple, Iterable

import pytest
from copr.v3 import Client
from fasjson_client import Client as FasjsonClient
from fasjson_client.errors import APIError
from flexmock import flexmock
from ogr.abstract import GitProject, GitService
from ogr.services.github import GithubProject, GithubService

import packit_service
from packit.api import PackitAPI
from packit.config import CommonPackageConfig, JobType, JobConfig, JobConfigTriggerType
from packit.copr_helper import CoprHelper
from packit.local_project import LocalProject
from packit_service.config import ServiceConfig
from packit_service.constants import (
    DOCS_APPROVAL_URL,
    NOTIFICATION_REPO,
)
from packit_service.models import (
    AllowlistModel as DBAllowlist,
    AllowlistStatus,
    PullRequestModel,
    JobTriggerModel,
    JobTriggerModelType,
)
from packit_service.worker.allowlist import Allowlist
from packit_service.worker.events import (
    EventData,
    IssueCommentEvent,
    PullRequestCommentGithubEvent,
    PullRequestGithubEvent,
    ReleaseEvent,
    AbstractGithubEvent,
)
from packit_service.worker.events.enums import (
    PullRequestAction,
    PullRequestCommentAction,
    IssueCommentAction,
)
from packit_service.worker.reporting import StatusReporter, BaseCommitStatus

EXPECTED_TESTING_FARM_CHECK_NAME = "testing-farm:fedora-rawhide-x86_64"


@pytest.fixture()
def allowlist():
    return Allowlist(service_config=ServiceConfig.get_service_config())


@pytest.fixture(scope="module")
def allowlist_entries():
    return {
        "github.com": flexmock(
            id=1, namespace="github.com", status=AllowlistStatus.denied.value
        ),
        "gitlab.com": flexmock(
            id=2, namespace="gitlab.com", status=AllowlistStatus.denied.value
        ),
        "github.com/fero": flexmock(
            id=3,
            namespace="github.com/fero",
            status=AllowlistStatus.approved_manually.value,
        ),
        "gitlab.com/lojzo": flexmock(
            id=4,
            namespace="gitlab.com/lojzo",
            status=AllowlistStatus.approved_automatically.value,
        ),
        "github.com/konipas": flexmock(
            id=5,
            namespace="github.com/konipas",
            status=AllowlistStatus.waiting.value,
        ),
        "gitlab.com/packit-service": flexmock(
            id=6,
            namespace="gitlab.com/packit-service",
            status=AllowlistStatus.denied.value,
        ),
        "gitlab.com/packit": flexmock(
            id=8,
            namespace="gitlab.com/packit",
            status=AllowlistStatus.approved_manually.value,
        ),
        "github.com/packit": flexmock(
            id=10,
            namespace="github.com/packit",
            status=AllowlistStatus.denied.value,
        ),
        "gitlab.com/packit-service/src": flexmock(
            id=7,
            namespace="gitlab.com/packit-service/src",
            status=AllowlistStatus.approved_automatically.value,
        ),
        "github.com/packit/packit.git": flexmock(
            id=9,
            namespace="github.com/packit/packit.git",
            status=AllowlistStatus.approved_manually.value,
        ),
        "gitlab.com/packit/packit.git": flexmock(
            id=11,
            namespace="gitlab.com/packit/packit.git",
            status=AllowlistStatus.denied.value,
        ),
        "github.com/packit/denied_packit.git": flexmock(
            id=12,
            namespace="github.com/packit/denied_packit.git",
            status=AllowlistStatus.denied.value,
        ),
        "github.com/fero/denied_packit.git": flexmock(
            id=13,
            namespace="github.com/fero/denied_packit.git",
            status=AllowlistStatus.denied.value,
        ),
    }


def mock_model(entries, namespaces):
    for queried_namespace in namespaces:
        (
            flexmock(DBAllowlist)
            .should_receive("get_namespace")
            .with_args(queried_namespace)
            .and_return(entries.get(queried_namespace))
            .ordered()
        )


@pytest.fixture()
def mocked_model(allowlist_entries, request):
    mock_model(allowlist_entries, request.param)


@pytest.mark.parametrize(
    "account_name, mocked_model, is_approved",
    (
        (  # Fero is approved
            "github.com/fero",
            ("github.com/fero",),
            True,
        ),
        (  # Lojzo is approved
            "gitlab.com/lojzo",
            ("gitlab.com/lojzo",),
            True,
        ),
        (  # Lojzo is approved on GitLab, shall not pass on GitHub
            "github.com/lojzo",
            ("github.com/lojzo", "github.com"),
            False,
        ),
        (  # Konipas is waiting... checks parent if approved(???)
            "github.com/konipas",
            ("github.com/konipas", "github.com"),
            False,
        ),
        (  # Krasomila is not present at all, checks parent
            "github.com/krasomila",
            ("github.com/krasomila", "github.com"),
            False,
        ),
        (  # gitlab.com/packit-service/src is approved
            "gitlab.com/packit-service/src/glibc.git",
            (
                "gitlab.com/packit-service/src/glibc.git",
                "gitlab.com/packit-service/src",
            ),
            True,
        ),
        (  # Approved on gitlab, shall not pass on GitHub
            "github.com/src/glibc.git",
            ("github.com/src/glibc.git", "github.com/src", "github.com"),
            False,
        ),
        (  # checks all the way up to the root: gitlab.com that is implicitly denied
            "gitlab.com/packit/packit.git",
            ("gitlab.com/packit/packit.git",),
            False,
        ),
        (  # packit.git is allowed on github, packit.git on gitlab is ignored
            "github.com/packit/packit.git",
            ("github.com/packit/packit.git",),
            True,
        ),
        (  # approved gitlab.com/packit
            "gitlab.com/packit/ogr.git",
            ("gitlab.com/packit/ogr.git", "gitlab.com/packit"),
            True,
        ),
    ),
    indirect=["mocked_model"],
)
def test_is_approved(allowlist, account_name, mocked_model, is_approved):
    assert allowlist.is_approved(account_name) == is_approved


@pytest.mark.parametrize(
    "event, mocked_model, approved",
    [
        (
            PullRequestCommentGithubEvent(
                action=PullRequestCommentAction.created,
                pr_id=0,
                base_repo_namespace="base",
                base_repo_name="",
                base_ref="",
                target_repo_namespace="foo",
                target_repo_name="bar",
                project_url="https://github.com/foo/bar",
                actor="bar",
                comment="",
                comment_id=0,
            ),
            ("github.com/foo/bar.git", "github.com/foo", "github.com"),
            False,
        ),
        (
            IssueCommentEvent(
                action=IssueCommentAction.created,
                issue_id=0,
                repo_namespace="foo",
                repo_name="bar",
                target_repo="",
                project_url="https://github.com/foo/bar",
                actor="baz",
                comment="",
                comment_id=0,
            ),
            ("github.com/foo/bar.git", "github.com/foo", "github.com"),
            False,
        ),
        (
            PullRequestCommentGithubEvent(
                action=PullRequestCommentAction.created,
                pr_id=0,
                base_repo_namespace="foo",
                base_repo_name="dwm",
                base_ref="",
                target_repo_namespace="fero",
                target_repo_name="dwm.git",
                project_url="https://github.com/fero/dwm",
                actor="lojzo",
                comment="",
                comment_id=0,
            ),
            ("github.com/fero/dwm.git", "github.com/fero"),
            True,
        ),
        (
            IssueCommentEvent(
                action=IssueCommentAction.created,
                issue_id=0,
                repo_namespace="packit-service/src",
                repo_name="glibc",
                target_repo="",
                project_url="https://gitlab.com/packit-service/src/glibc",
                actor="lojzo",
                comment="",
                comment_id=0,
            ),
            (
                "gitlab.com/packit-service/src/glibc.git",
                "gitlab.com/packit-service/src",
            ),
            True,
        ),
        (
            PullRequestCommentGithubEvent(
                action=PullRequestCommentAction.created,
                pr_id=0,
                base_repo_namespace="banned_namespace",
                base_repo_name="",
                base_ref="",
                target_repo_namespace="banned_namespace_again",
                target_repo_name="some_repo",
                project_url="https://github.com/banned_namespace_again/some_repo",
                actor="admin",
                comment="",
                comment_id=0,
            ),
            [],
            True,
        ),
    ],
    indirect=["mocked_model"],
)
def test_check_and_report_calls_method(allowlist, event, mocked_model, approved):
    gp = GitProject("", GitService(), "")

    flexmock(gp).should_receive("can_merge_pr").with_args(event.actor).and_return(
        approved
    )
    mocked_pr_or_issue = flexmock(author=None)
    if isinstance(event, IssueCommentEvent):
        flexmock(gp).should_receive("get_issue").and_return(mocked_pr_or_issue)
    else:
        flexmock(gp).should_receive("get_pr").and_return(mocked_pr_or_issue)
    expectation = mocked_pr_or_issue.should_receive("comment").with_args(
        "Project github.com/foo/bar.git is not on our allowlist!"
    )
    expectation.never() if approved else expectation.once()

    ServiceConfig.get_service_config().admins = {"admin"}
    assert (
        allowlist.check_and_report(
            event,
            gp,
            job_configs=[],
        )
        == approved
    )


@pytest.fixture()
def events(request) -> Iterable[Tuple[AbstractGithubEvent, bool, Iterable[str]]]:
    """
    :param request: event type to create Event instances of that type
    :return: list of Events that check_and_report accepts together with whether they should pass
    """
    types = {
        "release": (
            ReleaseEvent,
            lambda forge, namespace, repository: {
                "repo_namespace": namespace,
                "repo_name": repository,
                "tag_name": "",
                "project_url": f"https://{forge}/{namespace}/{repository}",
            },
        ),
        "pr": (
            PullRequestGithubEvent,
            lambda forge, namespace, repository: {
                "action": PullRequestAction.opened,
                "pr_id": 1,
                "base_repo_namespace": "",
                "base_repo_name": "",
                "target_repo_namespace": namespace,
                "target_repo_name": repository,
                "project_url": f"https://{forge}/{namespace}/{repository}",
                "commit_sha": "",
                "actor": "login",
                "base_ref": "",
            },
        ),
        "pr_comment": (
            PullRequestCommentGithubEvent,
            lambda forge, namespace, repository: {
                "action": PullRequestCommentAction.created,
                "pr_id": 1,
                "base_repo_namespace": namespace,
                "base_repo_name": "",
                "base_ref": "",
                "target_repo_namespace": namespace,
                "target_repo_name": repository,
                "project_url": f"https://{forge}/{namespace}/{repository}",
                "actor": "login",
                "comment": "",
                "comment_id": 1,
            },
        ),
        "issue_comment": (
            IssueCommentEvent,
            lambda forge, namespace, repository: {
                "action": IssueCommentAction.created,
                "issue_id": 1,
                "repo_namespace": namespace,
                "repo_name": repository,
                "target_repo": "",
                "project_url": f"https://{forge}/{namespace}/{repository}",
                "actor": "login",
                "comment": "",
                "comment_id": 1,
            },
        ),
        "admin": (
            PullRequestCommentGithubEvent,
            lambda forge, namespace, repository: {
                "action": PullRequestCommentAction.created,
                "pr_id": 1,
                "base_repo_namespace": "unapproved_namespace_override",
                "base_repo_name": "",
                "base_ref": "",
                "target_repo_namespace": namespace,
                "target_repo_name": repository,
                "project_url": f"https://{forge}/{namespace}/{repository}",
                "actor": "admin",
                "comment": "",
                "comment_id": 1,
            },
        ),
    }

    entries = [
        # could be turned into property-based if necessary
        # format: forge, namespace, repository, approved, resolved_through
        (
            "github.com",
            "fero",
            "awesome",
            True,
            ("github.com/fero/awesome.git", "github.com/fero"),
        ),
        (
            "github.com",
            "the-namespace",
            "the-repo",
            False,
            (
                "github.com/the-namespace/the-repo.git",
                "github.com/the-namespace",
                "github.com",
            ),
        ),
        (
            "github.com",
            "lojzo",
            "something",
            False,
            ("github.com/lojzo/something.git", "github.com/lojzo", "github.com"),
        ),
        (
            "github.com",
            "packit",
            "denied_packit",
            False,
            ("github.com/packit/denied_packit.git",),
        ),
        (
            "github.com",
            "fero",
            "denied_packit",
            False,
            ("github.com/fero/denied_packit.git",),
        ),
    ]

    event_type, args_factory = types[request.param]
    return (
        (
            event_type(**args_factory(forge, namespace, repository)),
            request.param == "admin" or approved,
            resolved_through if request.param != "admin" else [],
        )
        for forge, namespace, repository, approved, resolved_through in entries
    )


# https://stackoverflow.com/questions/35413134/what-does-indirect-true-false-in-pytest-mark-parametrize-do-mean
@pytest.mark.parametrize(
    "events",
    ["release", "pr", "pr_comment", "issue_comment", "admin"],
    indirect=True,
)
def test_check_and_report(
    allowlist: Allowlist,
    allowlist_entries,
    events: Iterable[Tuple[AbstractGithubEvent, bool, Iterable[str]]],
):
    """
    :param allowlist: fixture
    :param events: fixture: [(Event, should-be-approved)]
    """
    ServiceConfig.get_service_config().admins = {"admin"}
    flexmock(
        GithubProject,
        create_check_run=lambda *args, **kwargs: None,
        get_issue=lambda *args, **kwargs: flexmock(
            comment=lambda *args, **kwargs: None
        ),
        get_pr=lambda *args, **kwargs: flexmock(
            source_project=flexmock(), author=None, comment=lambda *args, **kwargs: None
        ),
    )
    job_configs = [
        JobConfig(
            type=JobType.tests,
            trigger=JobConfigTriggerType.pull_request,
            packages={
                "package": CommonPackageConfig(
                    _targets=["fedora-rawhide"],
                )
            },
        )
    ]
    flexmock(PullRequestGithubEvent).should_receive("get_package_config").and_return(
        flexmock(
            jobs=job_configs,
        )
    )
    flexmock(PullRequestModel).should_receive("get_or_create").and_return(
        flexmock(
            job_config_trigger_type=JobConfigTriggerType.pull_request,
            id=123,
            job_trigger_model_type=JobTriggerModelType.pull_request,
        )
    )

    flexmock(JobTriggerModel).should_receive("get_or_create").with_args(
        type=JobTriggerModelType.pull_request, trigger_id=123
    ).and_return(flexmock(id=2, type=JobTriggerModelType.pull_request))

    git_project = GithubProject("the-repo", GithubService(), "the-namespace")
    for event, is_valid, resolved_through in events:
        flexmock(GithubProject, can_merge_pr=lambda username: is_valid)
        flexmock(event, project=git_project).should_receive("get_dict").and_return(None)
        # needs to be included when running only `test_allowlist`
        # flexmock(event).should_receive("db_trigger").and_return(
        #     flexmock(job_config_trigger_type=job_configs[0].trigger).mock()
        # )
        flexmock(EventData).should_receive("from_event_dict").and_return(
            flexmock(commit_sha="0000000", pr_id="0")
        )

        if isinstance(event, PullRequestGithubEvent) and not is_valid:
            notification_project_mock = flexmock()
            notification_project_mock.should_receive("get_issue_list").with_args(
                author="packit-as-a-service[bot]"
            ).and_return(
                [
                    flexmock(title="something-different"),
                    flexmock(
                        title="Namespace the-namespace needs to be approved.",
                        url="https://issue.url",
                    ),
                    flexmock(title=""),
                ]
            )
            flexmock(ServiceConfig).should_receive("get_project").with_args(
                url=NOTIFICATION_REPO
            ).and_return(notification_project_mock)
            # Report the status
            flexmock(CoprHelper).should_receive("get_copr_client").and_return(
                Client(
                    config={
                        "copr_url": "https://copr.fedorainfracloud.org",
                        "username": "some-owner",
                    }
                )
            )
            flexmock(LocalProject).should_receive("refresh_the_arguments").and_return(
                None
            )
            flexmock(LocalProject).should_receive("checkout_pr").and_return(None)
            flexmock(StatusReporter).should_receive("report").with_args(
                description="github.com/the-namespace not allowed!",
                state=BaseCommitStatus.neutral,
                url="https://issue.url",
                check_names=[EXPECTED_TESTING_FARM_CHECK_NAME],
                markdown_content=(
                    "In order to start using the service, "
                    "your repository or namespace needs to be allowed. "
                    "We are now onboarding Fedora contributors who have "
                    "a valid [Fedora Account System](https://fedoraproject.org/wiki/Account_System)"
                    " account.\n\n"
                    "Packit has opened [an issue](https://issue.url) "
                    "for you to finish the approval process. "
                    "The process is automated and all the information can be found "
                    "in the linked issue.\n\n"
                    "For more details on how to get allowed for our service, please read "
                    "the instructions "
                    f"[in our onboarding guide]({DOCS_APPROVAL_URL})."
                ),
            ).once()
        flexmock(packit_service.worker.helpers.build.copr_build).should_receive(
            "get_valid_build_targets"
        ).and_return(
            {
                "fedora-rawhide-x86_64",
            }
        )
        mock_model(allowlist_entries, resolved_through)

        assert (
            allowlist.check_and_report(
                event,
                git_project,
                job_configs=job_configs,
            )
            is is_valid
        )


@pytest.mark.parametrize(
    "url, expected_url",
    [
        ("https://github.com/test/test_repo", "github.com/test/test_repo.git"),
        (
            "https://gitlab.somewhere.on.the.net/with/multiple/namespaces/repo.git",
            "gitlab.somewhere.on.the.net/with/multiple/namespaces/repo.git.git",
        ),
    ],
)
def test_strip_protocol_and_add_git(url: str, expected_url: str) -> None:
    assert Allowlist._strip_protocol_and_add_git(url) == expected_url


@pytest.mark.parametrize(
    "sender_login,fas_account_name,person_object,raises,result",
    [
        ("me", "me", {"github_username": "me"}, None, True),
        ("me", "me-fas", {"github_username": "me"}, None, True),
        ("you", "you", {"github_username": None}, None, False),
        ("she", "she", {"github_username": "me"}, None, False),
        ("they", "they", {}, APIError, False),
    ],
)
def test_is_github_username_from_fas_account_matching(
    sender_login, fas_account_name, person_object, raises, result
):
    flexmock(PackitAPI).should_receive("init_kerberos_ticket").and_return(True)

    def init(*args):
        pass

    # so that the kerberos authentication is not required
    FasjsonClient.__init__ = init
    # the Client class doesn't have directly the get_user method
    fas = (
        flexmock(FasjsonClient)
        .should_receive("__getattr__")
        .with_args("get_user")
        .once()
    )
    if person_object is not None:
        fas.and_return(flexmock(result=person_object))
    if raises is not None:
        fas.and_raise(raises)

    assert (
        Allowlist(
            service_config=flexmock()
        ).is_github_username_from_fas_account_matching(
            fas_account=fas_account_name, sender_login=sender_login
        )
        is result
    )
