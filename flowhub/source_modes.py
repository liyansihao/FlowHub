"""Source fulfillment eligibility shared by evaluation and publication."""


def supported_source_modes(value):
    modes = value.split(",") if isinstance(value, str) else value or ()
    return bool(modes) and all(str(mode).strip().upper() in {"FBO", "FBS"} for mode in modes)
