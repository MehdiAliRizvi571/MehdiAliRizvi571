"""Render profile stat cards as static SVGs from the GitHub GraphQL API.

Runs inside GitHub Actions with the repo's own GITHUB_TOKEN, so it does not
share an API quota with anyone else. Output goes to dist/ and is published to
the `output` branch; the README embeds the committed SVGs, never a live API.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape

API = "https://api.github.com/graphql"
TOKEN = os.environ["GITHUB_TOKEN"]
USER = os.environ["GITHUB_USER"]
OUT = "dist"

BG = "#0d1117"
BORDER = "#30363d"
TEXT = "#c9d1d9"
MUTED = "#8b949e"
ACCENT = "#00FF41"

FONT = (
    "'Segoe UI', Ubuntu, 'Helvetica Neue', Sans-Serif"
)

# Notebook files embed their output cells, so their byte count swamps every
# real language and says nothing about what the code is written in.
SKIP_LANGUAGES = {"Jupyter Notebook"}


def graphql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": f"{USER}-profile-stats",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"GitHub API HTTP {exc.code}: {exc.read().decode()[:500]}")
    if "errors" in payload:
        sys.exit(f"GitHub API errors: {payload['errors']}")
    return payload["data"]


PROFILE_Q = """
query($login: String!) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC) {
      totalCount
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

CALENDAR_Q = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch_days(start_year, this_year):
    """Contribution calendars max out at one year, so walk year by year."""
    days = {}
    totals = {"commits": 0, "prs": 0, "issues": 0, "contributions": 0}
    for year in range(start_year, this_year + 1):
        data = graphql(
            CALENDAR_Q,
            {
                "login": USER,
                "from": f"{year}-01-01T00:00:00Z",
                "to": f"{year}-12-31T23:59:59Z",
            },
        )["user"]["contributionsCollection"]
        totals["commits"] += data["totalCommitContributions"]
        totals["prs"] += data["totalPullRequestContributions"]
        totals["issues"] += data["totalIssueContributions"]
        totals["contributions"] += data["contributionCalendar"]["totalContributions"]
        for week in data["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                days[day["date"]] = day["contributionCount"]
    return days, totals


def streaks(days, today):
    """Current and longest run of consecutive days with >=1 contribution.

    A zero on today does not break the current streak: the day is not over yet.
    """
    if not days:
        return (0, None, None), (0, None, None)
    dates = sorted(days)
    first = date.fromisoformat(dates[0])

    longest = cur = 0
    longest_end = cur_start = None
    day = first
    while day <= today:
        if days.get(day.isoformat(), 0) > 0:
            cur += 1
            if cur == 1:
                cur_start = day
            if cur > longest:
                longest, longest_end = cur, day
        else:
            cur, cur_start = 0, None
        day += timedelta(days=1)

    # Re-walk backwards for the live streak, ignoring an empty today.
    end = today if days.get(today.isoformat(), 0) > 0 else today - timedelta(days=1)
    current, start = 0, None
    day = end
    while day >= first and days.get(day.isoformat(), 0) > 0:
        current += 1
        start = day
        day -= timedelta(days=1)

    longest_start = (
        longest_end - timedelta(days=longest - 1) if longest_end else None
    )
    return (current, start, end if current else None), (
        longest,
        longest_start,
        longest_end,
    )


def fmt(day):
    return f"{day.strftime('%b')} {day.day}, {day.year}" if day else "—"


def card(width, height, title, body):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">
  <style>
    .t {{ font: 600 16px {FONT}; fill: {ACCENT}; }}
    .k {{ font: 400 14px {FONT}; fill: {TEXT}; }}
    .v {{ font: 700 14px {FONT}; fill: {ACCENT}; }}
    .n {{ font: 800 30px {FONT}; fill: {TEXT}; }}
    .s {{ font: 600 12px {FONT}; fill: {ACCENT}; letter-spacing: .5px; }}
    .d {{ font: 400 11px {FONT}; fill: {MUTED}; }}
  </style>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="8" fill="{BG}" stroke="{BORDER}"/>
{body}
</svg>
"""


def overview(stats):
    rows = [
        ("Total Contributions", f"{stats['contributions']:,}"),
        ("Commits", f"{stats['commits']:,}"),
        ("Pull Requests", f"{stats['prs']:,}"),
        ("Issues", f"{stats['issues']:,}"),
        ("Stars Earned", f"{stats['stars']:,}"),
        ("Public Repos", f"{stats['repos']:,}"),
        ("Followers", f"{stats['followers']:,}"),
    ]
    lines = [f'  <text x="25" y="32" class="t">GitHub Overview</text>']
    y = 62
    for key, val in rows:
        lines.append(f'  <text x="25" y="{y}" class="k">{escape(key)}</text>')
        lines.append(f'  <text x="315" y="{y}" class="v" text-anchor="end">{val}</text>')
        y += 24
    return card(340, 240, "GitHub Overview", "\n".join(lines))


def languages(lang_repos):
    """Rank by how many repos a language appears in, not by bytes written.

    Bytes let one large web project outweigh everything else; repo count
    tracks what actually gets reached for across projects.
    """
    top = sorted(lang_repos.items(), key=lambda kv: (-kv[1]["repos"], kv[0]))[:6]
    total = sum(v["repos"] for _, v in top) or 1
    lines = [f'  <text x="25" y="32" class="t">Languages by Project</text>']

    x, bar_w = 25.0, 290.0
    for name, meta in top:
        w = bar_w * meta["repos"] / total
        lines.append(
            f'  <rect x="{x:.1f}" y="48" width="{w:.1f}" height="10" fill="{meta["color"] or MUTED}"/>'
        )
        x += w

    y = 84
    for i, (name, meta) in enumerate(top):
        col = 25 if i % 2 == 0 else 180
        if i % 2 == 0 and i:
            y += 26
        count = meta["repos"]
        label = f"{count} repo" if count == 1 else f"{count} repos"
        lines.append(
            f'  <circle cx="{col + 5}" cy="{y - 4}" r="5" fill="{meta["color"] or MUTED}"/>'
        )
        lines.append(
            f'  <text x="{col + 17}" y="{y}" class="k">{escape(name)} <tspan class="d">{label}</tspan></text>'
        )
    return card(340, 240, "Languages by Project", "\n".join(lines))


def streak_card(total, total_range, current, longest):
    cur_n, cur_s, cur_e = current
    long_n, long_s, long_e = longest
    cur_range = f"{fmt(cur_s)} - Present" if cur_n else "—"
    long_range = f"{fmt(long_s)} - {fmt(long_e)}" if long_n else "—"

    body = f"""  <line x1="235" y1="35" x2="235" y2="145" stroke="{BORDER}"/>
  <line x1="465" y1="35" x2="465" y2="145" stroke="{BORDER}"/>

  <text x="118" y="75" class="n" text-anchor="middle">{total:,}</text>
  <text x="118" y="102" class="s" text-anchor="middle">TOTAL CONTRIBUTIONS</text>
  <text x="118" y="122" class="d" text-anchor="middle">{escape(total_range)}</text>

  <circle cx="350" cy="72" r="34" fill="none" stroke="{ACCENT}" stroke-width="4"/>
  <text x="350" y="82" class="n" text-anchor="middle">{cur_n}</text>
  <text x="350" y="130" class="s" text-anchor="middle">CURRENT STREAK</text>
  <text x="350" y="150" class="d" text-anchor="middle">{escape(cur_range)}</text>

  <text x="582" y="75" class="n" text-anchor="middle">{long_n}</text>
  <text x="582" y="102" class="s" text-anchor="middle">LONGEST STREAK</text>
  <text x="582" y="122" class="d" text-anchor="middle">{escape(long_range)}</text>"""
    return card(700, 180, "Contribution streak", body)


if __name__ == "__main__":
    profile = graphql(PROFILE_Q, {"login": USER})["user"]
    created = datetime.fromisoformat(profile["createdAt"].replace("Z", "+00:00"))
    today = datetime.now(timezone.utc).date()

    days, totals = fetch_days(created.year, today.year)
    current, longest = streaks(days, today)

    active = sorted(d for d, c in days.items() if c > 0)
    total_range = (
        f"{fmt(date.fromisoformat(active[0]))} - Present" if active else "—"
    )

    lang_repos = {}
    stars = 0
    for repo in profile["repositories"]["nodes"]:
        stars += repo["stargazerCount"]
        for edge in repo["languages"]["edges"]:
            if edge["node"]["name"] in SKIP_LANGUAGES:
                continue
            entry = lang_repos.setdefault(
                edge["node"]["name"], {"repos": 0, "color": edge["node"]["color"]}
            )
            entry["repos"] += 1

    stats = {
        "contributions": totals["contributions"],
        "commits": totals["commits"],
        "prs": totals["prs"],
        "issues": totals["issues"],
        "stars": stars,
        "repos": profile["repositories"]["totalCount"],
        "followers": profile["followers"]["totalCount"],
    }

    os.makedirs(OUT, exist_ok=True)
    for name, svg in (
        ("stats.svg", overview(stats)),
        ("languages.svg", languages(lang_repos)),
        ("streak.svg", streak_card(totals["contributions"], total_range, current, longest)),
    ):
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(f"wrote {OUT}/{name}")
