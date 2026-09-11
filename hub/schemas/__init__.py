"""立场层 Schema（Pydantic v2）。

约定（后续新增代码沿用）：
- 枚举用 StrEnum；
- model_config = ConfigDict(extra="forbid")，禁止多余字段（防 mass assignment）；
- 入参（StanceCreate）与出参（StanceRead）分开。
"""
