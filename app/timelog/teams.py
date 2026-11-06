"""Teams: membership is the free-text `users.team`; the `teams` table holds per-team switches (on-call on/off).

A team without a row (or a user without a team) has every feature on, so nothing changes until an admin switches
on-call off for a team in Admin > Teams."""
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Team, User


def _key(name: str) -> str:
    return (name or "").strip().lower()


def team_rows(db: Session) -> Dict[str, Team]:
    return {_key(t.name): t for t in db.scalars(select(Team))}


def team_oncall_enabled(db: Session, name: str) -> bool:
    row = team_rows(db).get(_key(name))
    return row is None or row.oncall_enabled


def oncall_enabled(db: Session, user: User) -> bool:
    """Whether `user` declares on-call duty (their team has it switched on)."""
    return team_oncall_enabled(db, user.team)


def oncall_disabled_message(user: User) -> str:
    return f"On-call is not enabled for the team {user.team}: it declares overtime only."


def all_team_names(db: Session, include_archived: bool = False) -> List[str]:
    """Team names known to the application: used by users plus those created in Admin > Teams."""
    stmt = select(User.team)
    if not include_archived:
        stmt = stmt.where(User.is_archived.is_not(True))
    names: Dict[str, str] = {}
    for n in list(db.scalars(stmt)) + [t.name for t in db.scalars(select(Team))]:
        if n and n.strip():
            names.setdefault(_key(n), n.strip())
    return sorted(names.values(), key=str.lower)


def expert_conflict(db: Session, team: str, is_expert: bool) -> Optional[str]:
    """Experts only declare on-call, so they cannot belong to a team that has it switched off."""
    if is_expert and not team_oncall_enabled(db, team):
        return f"Experts only declare on-call, and on-call is switched off for the team {team}."
    return None
