from ollie_hands.mode import BYPASS, NORMAL, Mode


def test_mode_defaults_to_normal_and_accepts_exact_values():
    mode = Mode()
    assert mode.get() == NORMAL
    assert mode.is_bypass() is False
    assert mode.set(BYPASS) == BYPASS
    assert mode.is_bypass() is True
    assert mode.set(NORMAL) == NORMAL


def test_mode_rejects_every_other_value_without_changing_state():
    mode = Mode()
    for value in ("", "NORMAL", " bypass", "autonomous", None):
        try:
            mode.set(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid mode {value!r}")
        assert mode.get() == NORMAL


def test_new_controller_resets_to_normal():
    first = Mode()
    first.set(BYPASS)
    assert Mode().get() == NORMAL
