# Cases Patch 格式

## 何时使用

步骤 3b 写入 cases-patch JSON 文件，步骤 4 通过 `runner.py run --cases-patch <path>` 自动注册并同步 case 状态。

## 完整格式

```json
{
  "files": {
    "core/parser.py": {
      "test_path": "test/generated_unit/core/test_parser.py",
      "functions": {
        "parse_header": {
          "cases": [
            {
              "id": "functional_01",
              "dimension": "functional",
              "description": "有效 header 返回解析后 Header 对象",
              "test_name": "test_parse_header_valid",
              "status": "pending"
            },
            {
              "id": "boundary_01",
              "dimension": "boundary",
              "description": "空输入返回 None",
              "test_name": "test_parse_header_empty",
              "status": "pending"
            }
          ]
        },
        "validate_input": {
          "cases": [
            {
              "id": "functional_01",
              "dimension": "functional",
              "description": "合法输入返回 True",
              "test_name": "test_validate_input_valid",
              "status": "pending"
            }
          ]
        }
      }
    }
  }
}
```

## 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | Case 唯一标识，格式 `{dimension}_{序号}`，如 `functional_01` |
| `dimension` | string | 测试维度：`functional` / `boundary` / `exception` / `data_integrity` 等 |
| `description` | string | 一句话描述测试目的 |
| `test_name` | string | Python 测试函数名（如 `test_parse_header_valid`），不是 case ID |
| `status` | string | 初始注册时填 `"pending"`；`update-state` 带 `--run-result` 时会自动同步为 `passed`/`failed` |

## 注意事项

- `test_name` 是 Python 测试函数名，必须与测试文件中的 `def test_xxx()` 一致
- 迭代 N > 1 时只包含本轮新增的 case，不要重复已有 case
- `--run-result` 可选：首次注册（跑测试前）可不传；跑完测试后再调一次时传入即可同步状态
