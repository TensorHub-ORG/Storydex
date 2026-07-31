"""Authoritative call accounting for the story generation path.

Three numbers describe one turn and must not be conflated:

* ``logicalStoryCalls`` counts prose calls the pipeline chose to make;
* ``providerAttempts`` counts actual Provider requests, transport retries
  included;
* ``transportRetries`` counts only the retries inside those attempts.

The HTTP request count must never stand in for the logical call count. A single
logical call that retries twice on a 429 is still one call against the budget,
and reporting three would make a healthy turn look over budget. The inverse
mistake is worse: counting one logical call while the chain quietly spent a
second prose call is exactly the accounting hole this module closes.

Non-prose Provider work (intent classification, memory recall, planning, chat)
is recorded under its own purpose and never enters ``logicalStoryCalls``. Those
calls already existed and must not grow because length control was switched on,
so they are tracked separately rather than hidden or merged.
"""

from __future__ import annotations

from typing import Any, Dict

STORY_INITIAL_GENERATION_PURPOSE = "story_initial_generation"
STORY_LENGTH_REVISION_PURPOSE = "story_length_revision"
STORY_SECOND_DRAFT_PURPOSE = "story_second_draft"
# Only these purposes are prose calls against the turn budget.
STORY_PROSE_PURPOSES = (
    STORY_INITIAL_GENERATION_PURPOSE,
    STORY_LENGTH_REVISION_PURPOSE,
    STORY_SECOND_DRAFT_PURPOSE,
)

# Every mode gets exactly one draft. Length control may add at most one revision
# or one independent second draft, so a turn never exceeds two prose calls.
REQUIRED_INITIAL_STORY_CALLS = 1
MAXIMUM_LENGTH_REVISION_CALLS = 1


def is_story_prose_purpose(purpose: Any) -> bool:
    return str(purpose or "").strip() in STORY_PROSE_PURPOSES


class StoryCallAccounting:
    """Collect the three call numbers for one story generation turn."""

    def __init__(self) -> None:
        self.logical_story_calls = 0
        self.provider_attempts = 0
        self.transport_retries = 0
        self._logical_by_purpose: Dict[str, int] = {}
        self._attempts_by_purpose: Dict[str, int] = {}
        self._retries_by_purpose: Dict[str, int] = {}

    def record_logical_call(self, purpose: str) -> None:
        """Record one logical model call about to run."""

        key = self._normalize(purpose)
        self._logical_by_purpose[key] = self._logical_by_purpose.get(key, 0) + 1
        if is_story_prose_purpose(key):
            self.logical_story_calls += 1

    def record_provider_attempt(self, purpose: str) -> None:
        """Record one real Provider request, including retried ones."""

        key = self._normalize(purpose)
        self._attempts_by_purpose[key] = self._attempts_by_purpose.get(key, 0) + 1
        self.provider_attempts += 1

    def record_transport_retry(self, purpose: str) -> None:
        key = self._normalize(purpose)
        self._retries_by_purpose[key] = self._retries_by_purpose.get(key, 0) + 1
        self.transport_retries += 1

    def logical_calls_for(self, purpose: str) -> int:
        return int(self._logical_by_purpose.get(self._normalize(purpose), 0))

    def provider_attempts_for(self, purpose: str) -> int:
        return int(self._attempts_by_purpose.get(self._normalize(purpose), 0))

    def transport_retries_for(self, purpose: str) -> int:
        return int(self._retries_by_purpose.get(self._normalize(purpose), 0))

    @property
    def initial_generation_calls(self) -> int:
        return self.logical_calls_for(STORY_INITIAL_GENERATION_PURPOSE)

    @property
    def length_revision_calls(self) -> int:
        return self.logical_calls_for(STORY_LENGTH_REVISION_PURPOSE)

    @property
    def second_draft_calls(self) -> int:
        return self.logical_calls_for(STORY_SECOND_DRAFT_PURPOSE)

    @property
    def non_prose_calls(self) -> Dict[str, int]:
        """Report other Provider purposes so growth there stays visible."""

        return {
            purpose: count
            for purpose, count in sorted(self._logical_by_purpose.items())
            if not is_story_prose_purpose(purpose)
        }

    def payload(self) -> Dict[str, Any]:
        return {
            "logicalStoryCalls": self.logical_story_calls,
            "providerAttempts": self.provider_attempts,
            "transportRetries": self.transport_retries,
            "initialGenerationCalls": self.initial_generation_calls,
            "lengthRevisionCalls": self.length_revision_calls,
            "secondDraftCalls": self.second_draft_calls,
            "nonProseCalls": dict(self.non_prose_calls),
        }

    def contract_violations(
        self,
        *,
        precision_enabled: bool,
        asymmetric_enabled: bool = False,
    ) -> list[str]:
        """List the ways this turn broke the call contract.

        An empty list is the only acceptable audit result. The checks stay
        separate so a report names what actually went wrong instead of just
        saying the budget was exceeded.
        """

        violations: list[str] = []
        initial = self.initial_generation_calls
        revisions = self.length_revision_calls
        second_drafts = self.second_draft_calls
        if initial != REQUIRED_INITIAL_STORY_CALLS:
            violations.append(
                f"expected exactly {REQUIRED_INITIAL_STORY_CALLS} "
                f"{STORY_INITIAL_GENERATION_PURPOSE} call, recorded {initial}"
            )
        if not precision_enabled and revisions:
            violations.append(
                f"recorded {revisions} {STORY_LENGTH_REVISION_PURPOSE} call(s) "
                "while precise word count was disabled"
            )
        if revisions > MAXIMUM_LENGTH_REVISION_CALLS:
            violations.append(
                f"recorded {revisions} {STORY_LENGTH_REVISION_PURPOSE} calls, "
                f"at most {MAXIMUM_LENGTH_REVISION_CALLS} is allowed"
            )
        if not asymmetric_enabled and second_drafts:
            violations.append(
                f"recorded {second_drafts} {STORY_SECOND_DRAFT_PURPOSE} call(s) "
                "while asymmetric story length was disabled"
            )
        if second_drafts > MAXIMUM_LENGTH_REVISION_CALLS:
            violations.append(
                f"recorded {second_drafts} {STORY_SECOND_DRAFT_PURPOSE} calls, "
                f"at most {MAXIMUM_LENGTH_REVISION_CALLS} is allowed"
            )
        if precision_enabled and asymmetric_enabled:
            violations.append(
                "precision revision and asymmetric second draft were enabled together"
            )
        if self.logical_story_calls != initial + revisions + second_drafts:
            violations.append(
                f"logicalStoryCalls {self.logical_story_calls} does not equal "
                f"initial {initial} plus revision {revisions} plus second draft {second_drafts}"
            )
        # A revision that retries transport turns one precise correction into
        # three or four requests, so it is budgeted at zero retries.
        revision_retries = self.transport_retries_for(STORY_LENGTH_REVISION_PURPOSE)
        if revision_retries:
            violations.append(
                f"recorded {revision_retries} transport retries on "
                f"{STORY_LENGTH_REVISION_PURPOSE}, which allows none"
            )
        second_draft_retries = self.transport_retries_for(STORY_SECOND_DRAFT_PURPOSE)
        if second_draft_retries:
            violations.append(
                f"recorded {second_draft_retries} transport retries on "
                f"{STORY_SECOND_DRAFT_PURPOSE}, which allows none"
            )
        if self.provider_attempts < self.logical_story_calls:
            violations.append(
                f"providerAttempts {self.provider_attempts} is below "
                f"logicalStoryCalls {self.logical_story_calls}"
            )
        return violations

    @staticmethod
    def _normalize(purpose: Any) -> str:
        return str(purpose or "").strip() or "unknown"
