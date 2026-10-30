import time

import httpx
import pytest

from bot.errors import UserError
from bot.services.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubNotConfigured,
    GitHubNotFound,
    GitHubRateLimited,
    GitHubUnavailable,
    normalize_login,
    normalize_repo,
)

REPO = "team/project"


def issue_json(number: int, state: str = "open", **kw) -> dict:
    return {
        "number": number,
        "title": kw.get("title", f"Issue {number}"),
        "state": state,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "assignees": [{"login": login} for login in kw.get("assignees", [])],
        "labels": [{"name": name} for name in kw.get("labels", [])],
        **({"pull_request": {"url": "x"}} if kw.get("pr") else {}),
    }


def client_for(handler) -> GitHubClient:
    return GitHubClient("test-token", transport=httpx.MockTransport(handler))


async def test_etag_conditional_requests_reuse_cache():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=issue_json(5), headers={"etag": '"v1"'})

    gh = client_for(handler)
    first = await gh.get_issue(REPO, 5)
    second = await gh.get_issue(REPO, 5)
    assert first == second and first.number == 5
    assert calls == [None, '"v1"']
    await gh.close()


async def test_auth_header_and_api_version():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=issue_json(1))

    gh = client_for(handler)
    await gh.get_issue(REPO, 1)
    assert seen["authorization"] == "Bearer test-token"
    assert seen["x-github-api-version"] == "2022-11-28"
    await gh.close()


async def test_rate_limit_blocks_further_requests():
    calls = []
    reset = int(time.time()) + 120

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
        )

    gh = client_for(handler)
    with pytest.raises(GitHubRateLimited):
        await gh.get_issue(REPO, 1)
    with pytest.raises(GitHubRateLimited):
        await gh.get_issue(REPO, 2)
    assert len(calls) == 1  # second call did not hit the API
    assert gh.blocked_until == reset
    await gh.close()


async def test_secondary_rate_limit_429_with_retry_after():
    gh = client_for(lambda r: httpx.Response(429, headers={"retry-after": "30"}))
    with pytest.raises(GitHubRateLimited):
        await gh.get_issue(REPO, 1)
    assert 25 < gh.blocked_until - time.time() <= 31
    await gh.close()


@pytest.mark.parametrize(
    ("status", "exc"),
    [(404, GitHubNotFound), (401, GitHubAuthError), (502, GitHubUnavailable)],
)
async def test_error_mapping(status, exc):
    gh = client_for(lambda r: httpx.Response(status, json={"message": "x"}))
    with pytest.raises(exc):
        await gh.get_issue(REPO, 1)
    await gh.close()


async def test_network_error_is_user_facing():
    def handler(request):
        raise httpx.ConnectError("boom")

    gh = client_for(handler)
    with pytest.raises(GitHubUnavailable):
        await gh.list_open_issues(REPO)
    await gh.close()


async def test_not_configured():
    gh = GitHubClient(None)
    with pytest.raises(GitHubNotConfigured):
        await gh.get_issue(REPO, 1)
    await gh.close()


@pytest.mark.parametrize("status", [200, 404])
async def test_public_profile_can_be_checked_without_token(status):
    def handler(request):
        assert str(request.url) == "https://api.github.com/users/Aydar-art"
        assert "authorization" not in request.headers
        return httpx.Response(status, json={"login": "Aydar-art"})

    gh = GitHubClient(None, transport=httpx.MockTransport(handler))
    try:
        assert await gh.user_exists("https://github.com/Aydar-art") is (status == 200)
        assert not gh.configured
        with pytest.raises(GitHubNotConfigured):
            await gh.add_assignees(REPO, 1, ["Aydar-art"])
        with pytest.raises(GitHubNotConfigured):
            await gh.get_repo(REPO)
    finally:
        await gh.close()


async def test_public_profile_lookup_still_obeys_rate_limits():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "30"})

    gh = GitHubClient(None, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubRateLimited):
            await gh.user_exists("Aydar-art")
        with pytest.raises(GitHubRateLimited):
            await gh.user_exists("Aydar-art")
        assert len(calls) == 1
    finally:
        await gh.close()


@pytest.mark.parametrize(
    "login", ["../repos/team/project", "Aydar-art/repos", "https://evil.example/x"]
)
async def test_invalid_profile_lookup_never_sends_a_request(login):
    def handler(request):
        pytest.fail("Invalid account must not reach GitHub")

    gh = GitHubClient(None, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UserError):
            await gh.user_exists(login)
    finally:
        await gh.close()


async def test_pull_request_is_not_an_issue():
    gh = client_for(lambda r: httpx.Response(200, json=issue_json(3, pr=True)))
    with pytest.raises(GitHubNotFound):
        await gh.get_issue(REPO, 3)
    await gh.close()


async def test_list_open_issues_paginates_and_skips_prs():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[issue_json(3)])
        assert request.url.params["state"] == "open"
        next_url = f"https://api.github.com/repos/{REPO}/issues?state=open&page=2"
        return httpx.Response(
            200,
            json=[issue_json(1, assignees=["Alice"]), issue_json(2, pr=True)],
            headers={"link": f'<{next_url}>; rel="next"'},
        )

    gh = client_for(handler)
    issues = await gh.list_open_issues(REPO)
    assert [i.number for i in issues] == [1, 3]
    assert issues[0].assignees == ("Alice",)
    await gh.close()


async def test_linked_open_pr_detection():
    events = [
        {"event": "labeled"},
        {"event": "cross-referenced", "source": {"issue": {"state": "closed", "pull_request": {}}}},
        {"event": "cross-referenced", "source": {"issue": {"state": "open"}}},  # plain issue
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=list(events))

    gh = client_for(handler)
    assert not await gh.has_open_linked_pr(REPO, 1)
    events.append(
        {
            "event": "cross-referenced",
            "source": {"issue": {"state": "open", "pull_request": {"url": "x"}}},
        }
    )
    gh._etags.clear()
    assert await gh.has_open_linked_pr(REPO, 1)
    await gh.close()


async def test_write_operations_are_limited_to_issue_creation_and_assignees():
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        return httpx.Response(201, json=issue_json(9, assignees=["bob"]))

    gh = client_for(handler)
    await gh.create_issue(REPO, "t", "b")
    await gh.add_assignees(REPO, 9, ["bob"])
    await gh.remove_assignees(REPO, 9, ["bob"])
    assert methods == [
        ("POST", f"/repos/{REPO}/issues"),
        ("POST", f"/repos/{REPO}/issues/9/assignees"),
        ("DELETE", f"/repos/{REPO}/issues/9/assignees"),
    ]
    public = {name for name in dir(GitHubClient) if not name.startswith("_")}
    assert public == {
        "configured",
        "close",
        "get_repo",
        "user_exists",
        "get_issue",
        "list_open_issues",
        "has_open_linked_pr",
        "create_issue",
        "add_assignees",
        "remove_assignees",
    }
    await gh.close()


def test_normalize_repo():
    assert normalize_repo("team/project") == "team/project"
    assert normalize_repo("https://github.com/team/project.git") == "team/project"
    assert normalize_repo("github.com/team/my.repo/") == "team/my.repo"
    for bad in ("team", "team/pro/ject", "../etc", "team/proj ect", "https://gitlab.com/a/b"):
        with pytest.raises(UserError):
            normalize_repo(bad)


def test_normalize_login():
    assert normalize_login("@octo-cat") == "octo-cat"
    assert normalize_login("https://github.com/octocat/") == "octocat"
    for bad in ("-bad", "a b", "x" * 40, "a/b"):
        with pytest.raises(UserError):
            normalize_login(bad)


@pytest.mark.parametrize(
    "next_url",
    [
        f"https://attacker.example/repos/{REPO}/issues?page=2",
        f"http://api.github.com/repos/{REPO}/issues?page=2",
        "https://api.github.com/user",
        f"https://user:password@api.github.com/repos/{REPO}/issues?page=2",
        "https://api.github.com/repos/other/project/issues?page=2",
    ],
)
async def test_unsafe_pagination_is_blocked_before_sending_credentials(next_url):
    from bot.services.github import GitHubError

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json=[issue_json(1)], headers={"link": f'<{next_url}>; rel="next"'}
        )

    gh = client_for(handler)
    try:
        with pytest.raises(GitHubError):
            await gh.list_open_issues(REPO)
        assert len(requests) == 1
        assert requests[0].url.host == "api.github.com"
    finally:
        await gh.close()


async def test_github_redirects_are_not_followed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.example/token"})

    gh = client_for(handler)
    try:
        with pytest.raises(GitHubUnavailable):
            await gh.get_issue(REPO, 1)
        assert len(requests) == 1
    finally:
        await gh.close()


@pytest.mark.parametrize("repo", ["team/.", "team/..", "https://github.com/team/.."])
def test_repository_path_traversal_is_rejected(repo):
    with pytest.raises(UserError):
        normalize_repo(repo)
