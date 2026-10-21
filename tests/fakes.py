from unittest.mock import AsyncMock
class FakeLLM:
    def __init__(self):
        self.chat = AsyncMock(return_value='Test evidence [1]')
        self.extract_structured = AsyncMock(return_value={})
