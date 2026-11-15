"""Match every profile against every scholarship that is not closed, and save the results.

The matching itself (match_all) is pure; run_matching loads from and saves to Postgres.
"""

from dataclasses import dataclass
from datetime import date

from pipeline import db
from pipeline.matching.rules import RuleResult, ScholarshipFacts, Status, evaluate, overall_status
from pipeline.matching.score import match_score
from pipeline.profile import Profile


@dataclass
class Match:
    profile_id: str
    profile_name: str
    scholarship: ScholarshipFacts
    status: Status
    score: int
    reasons: list[RuleResult]


def match_one(
    profile_id: str, profile: Profile, scholarship: ScholarshipFacts, today: date
) -> Match:
    reasons = evaluate(profile, scholarship, today)
    status = overall_status(reasons)
    return Match(profile_id, profile.full_name, scholarship, status,
                 match_score(scholarship, status, today), reasons)  # fmt: skip


def match_all(
    profiles: list[tuple[str, Profile]], scholarships: list[ScholarshipFacts], today: date
) -> list[Match]:
    """Every (profile, open scholarship) pair, best score first."""
    open_ones = [s for s in scholarships if s.status != "closed"]
    matches = [match_one(pid, profile, s, today) for pid, profile in profiles for s in open_ones]
    return sorted(matches, key=lambda m: (m.profile_name, -m.score, m.scholarship.name))


def run_matching(conn: db.Conn, today: date | None = None) -> list[Match]:
    """Load profiles and open scholarships, match them, and upsert the matches table."""
    today = today or date.today()
    matches = match_all(db.load_profiles(conn), db.load_open_scholarships(conn), today)
    db.save_matches(conn, [
        db.MatchRow(m.profile_id, m.scholarship.id or "", m.status, m.score,
                    [r.as_json() for r in m.reasons])
        for m in matches
    ])  # fmt: skip
    return matches
