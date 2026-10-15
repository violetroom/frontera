"""In-memory GitHub and forge fakes for tracker, runner and action tests."""

import re
from itertools import count
from urllib.parse import parse_qs, unquote, urlparse

REPO = "violetroom/Shiroe"


class FakeGh:
    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.perms = {"owner": "admin"}
        self.pulls = {}
        self.calls = []
        self._ids = count(1000)

    def add_issue(self, number, title="Fix a thing", body="", labels=(), state="open"):
        self.issues[number] = {"number": number, "title": title, "body": body, "state": state,
                               "labels": [{"name": l} for l in labels]}
        self.comments.setdefault(number, [])

    def labels(self, number):
        return [l["name"] for l in self.issues[number]["labels"]]

    def add_comment(self, number, body, login="github-actions[bot]"):
        c = {"id": next(self._ids), "user": {"login": login}, "body": body}
        self.comments.setdefault(number, []).append(c)
        return c

    def api(self, method, path, body=None):
        self.calls.append((method, path, body))
        u = urlparse(path)
        p = u.path.removeprefix(f"repos/{REPO}/")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if method == "GET" and (m := re.fullmatch(r"issues/(\d+)", p)):
            return self.issues[int(m.group(1))]
        if method == "GET" and (m := re.fullmatch(r"pulls/(\d+)", p)):
            return self.pulls[int(m.group(1))]
        if method == "GET" and (m := re.fullmatch(r"issues/(\d+)/comments", p)):
            return self.comments.get(int(m.group(1)), [])
        if method == "GET" and p == "issues":
            want = q.get("labels")
            return [i for i in self.issues.values()
                    if i["state"] == q.get("state", "open") and (not want or want in [l["name"] for l in i["labels"]])]
        if method == "DELETE" and (m := re.fullmatch(r"issues/(\d+)/labels/(.+)", p)):
            i = self.issues[int(m.group(1))]
            i["labels"] = [l for l in i["labels"] if l["name"] != unquote(m.group(2))]
            return None
        if method == "POST" and (m := re.fullmatch(r"issues/(\d+)/labels", p)):
            i = self.issues[int(m.group(1))]
            for name in body["labels"]:
                if name not in self.labels(i["number"]):
                    i["labels"].append({"name": name})
            return None
        if method == "POST" and (m := re.fullmatch(r"issues/(\d+)/comments", p)):
            return self.add_comment(int(m.group(1)), body["body"])
        if method == "PATCH" and (m := re.fullmatch(r"issues/comments/(\d+)", p)):
            for cs in self.comments.values():
                for c in cs:
                    if c["id"] == int(m.group(1)):
                        c["body"] = body["body"]
                        return c
        if method == "PATCH" and (m := re.fullmatch(r"issues/(\d+)", p)):
            i = self.issues[int(m.group(1))]
            i["state"] = body.get("state", i["state"])
            if "state_reason" in body:
                i["state_reason"] = body["state_reason"]
            return i
        if method == "GET" and (m := re.fullmatch(r"collaborators/(.+)/permission", p)):
            role = self.perms.get(unquote(m.group(1)), "none")
            return {"permission": "admin" if role == "admin" else role, "role_name": role}
        raise AssertionError(f"unexpected gh call {method} {path}")

    def bot_comments(self, number):
        return [c["body"] for c in self.comments.get(number, []) if c["user"]["login"] == "github-actions[bot]"]


class FakeForge:
    def __init__(self, repo=None):
        self.repo = repo  # when set, pushes go to the repo's real (local) origin
        self.pushed, self.prs, self.closed, self.reviews, self.pr_comments, self.merged = [], [], [], [], [], []
        self.merge_result = (True, "merged")

    def push(self, branch):
        self.pushed.append(branch)
        if self.repo is not None:
            import subprocess
            subprocess.run(["git", "-C", str(self.repo), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"],
                           check=True, capture_output=True)

    def comment_pr(self, number, body):
        self.pr_comments.append((number, body))

    def merge_pr(self, number, branch):
        if self.merge_result[0]:
            self.merged.append(number)
        return self.merge_result

    def open_pr(self, title, body, head, base):
        self.prs.append({"title": title, "body": body, "head": head, "base": base})
        return 100 + len(self.prs)

    def close_pr(self, number, comment):
        self.closed.append(number)

    def review_pr(self, number, body):
        self.reviews.append((number, body))
