"""Development example only. Do not mark synthetic evidence as real supplier verification."""


async def invoke(operation: str, context: dict, credential: str) -> dict:
    if operation != "match":
        raise ValueError("unsupported operation")
    # Implement your provider here, returning the fields in docs/MODULE_PROTOCOL.md.
    # Raise an error when the provider is unavailable; never invent a score or cost.
    raise NotImplementedError("connect a real matching provider before enabling this plugin")
