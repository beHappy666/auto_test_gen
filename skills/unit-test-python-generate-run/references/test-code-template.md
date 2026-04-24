# Python 测试代码模板

## 文件结构

```python
# test/generated_unit/<src>/test_<name>.py
import pytest
from unittest.mock import patch, MagicMock
from <module path> import <function/class>
```

## CASE_ID 格式（解析硬依赖）

每个测试函数**上方**必须有 `# CASE_ID: <id>` 注释，`analyze.py` 通过正则 `#\s*CASE_ID\s*:\s*([A-Za-z0-9_\-]+)` 解析。

```python
# CASE_ID: functional_01
def test_parse_header_valid():
    """有效 header 返回解析后 Header 对象"""
    result = parse_header(b"HTTP/1.1 200 OK\r\n")
    assert result.status_code == 200
```

注意：注释和 `def` 之间不能有其他非空行（装饰器除外）。

## 测试风格示例

### 功能测试

```python
# CASE_ID: functional_01
def test_parse_header_valid():
    result = parse_header(b"HTTP/1.1 200 OK\r\n")
    assert result.status_code == 200
```

### 边界测试（参数化）

```python
# CASE_ID: boundary_01
@pytest.mark.parametrize("data", [b"", b"\x00", b"X" * 65536])
def test_parse_header_boundary(data):
    try:
        parse_header(data)
    except (ValueError, TypeError):
        pass
```

### 异常测试

```python
# CASE_ID: exception_01
def test_parse_header_malformed():
    with pytest.raises(ValueError):
        parse_header(b"NOT HTTP")
```

---

## Mock 策略

### 1. `unittest.mock.patch` — 替换模块级函数或类方法

```python
with patch('module.network_request') as mock_req:
    mock_req.return_value = Response(200, "OK")
    result = process()
```

### 2. `MagicMock` — 创建 mock 对象用于依赖注入

```python
mock_db = MagicMock()
mock_db.query.return_value = [{"id": 1}]
service = MyService(db=mock_db)
```

### 3. `patch.object` — 替换对象特定属性/方法

```python
with patch.object(instance, 'method', return_value=42):
    result = instance.run()
```

### 4. `PropertyMock` — mock 属性（property）

```python
from unittest.mock import PropertyMock

with patch('Module.Class.property', new_callable=PropertyMock, return_value="value"):
    ...
```

### Mock 注意事项

- mock 的 target 路径必须在源码中确实存在（如 `patch('module.func')` 要求 `module.func` 是真实的引用点）
- 对编排函数（`main()`、`run_executor()`），需逐层 mock 子函数才能覆盖各分支
- 使用 `return_value` / `side_effect` 控制返回值和异常

---

## conftest.py

如果 `test/generated_unit/conftest.py` 缺失且出现 import 错误，创建最小 conftest.py：

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
```

## 编码规范

| 场景 | 做法 |
|------|------|
| 浮点比较 | `pytest.approx` 而非 `==` |
| dict 比较 | 不断言 key 顺序，用 `==` 比较内容 |
| 路径拼接 | 用 `pathlib` 或 `os.path`，不硬编码 `/` 或 `\` |
| 时间相关 | `datetime.now()` 必须用 `freezegun` 或 mock freeze |
| 临时文件 | 用 pytest 内置 `tmp_path` fixture（xdist 并行时每个 worker 有独立目录） |
