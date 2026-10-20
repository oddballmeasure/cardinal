from cardinal.config.models import Scout
from cardinal.scout import gate


def settings():
    return Scout(autonomy="auto", categories=["feature"], areas_per_pass=1, cooldown_days=0,
                 max_proposals_per_pass=1, max_files=1, repro=False, auto_min_decided=1,
                 auto_min_approval=0.8, auto_min_done=0)


def test_zero_min_done_with_no_landed_issues_goes_auto_without_raising():
    record = {"feature": {"decided": 2, "approval": 1.0, "done_rate": None}}
    mode, why = gate.decide(settings(), record, "feature", None)
    assert mode == "auto", why
    assert "no landed issues" in why, why
    assert "None" not in why, why

    record = {"feature": {"decided": 2, "approval": 1.0, "done_rate": 0.5}}
    mode, why = gate.decide(settings(), record, "feature", None)
    assert mode == "auto", why
    assert "50% of landed ones done" in why, why
