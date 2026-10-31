"""Small numeric expression language for task-owned speed-density laws.

Expressions are interpreted from a bounded AST. Python execution, attributes,
indexing and arbitrary function calls are never accepted.
"""
import ast
import copy
import hashlib
import json
import math
import re

VARIABLES = {'rho', 'v0', 'rho_max'}
FUNCTIONS = {'exp': 1, 'log': 1, 'sqrt': 1, 'abs': 1, 'min': 2, 'max': 2, 'clip': 3, 'where': 3}
MAX_METHODS = 6


class SpeedFormula:
    def __init__(self, expression, parameters):
        self.parameters = {item['name']: item['value'] for item in parameters}
        try:
            self.tree = ast.parse(expression, mode='eval').body
        except (SyntaxError, RecursionError, MemoryError):
            raise ValueError('公式语法无效；请使用表达式，例如 v0 * max(0, 1 - rho / rho_max)') from None
        if len(list(ast.walk(self.tree))) > 160:
            raise ValueError('公式过于复杂，最多允许160个语法节点')
        if isinstance(self.tree, ast.Compare):
            raise ValueError('公式必须输出速度；比较条件请放在where函数或数值表达式中')
        self.check(self.tree, 0)

    def check(self, node, depth):
        if depth > 24:
            raise ValueError('公式嵌套过深')
        if isinstance(node, ast.Constant) and type(node.value) in (int, float) and abs(node.value) <= 1e6 and math.isfinite(node.value):
            return
        if isinstance(node, ast.Name) and node.id in VARIABLES | set(self.parameters):
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            self.check(node.operand, depth + 1)
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            self.check(node.left, depth + 1); self.check(node.right, depth + 1)
            if isinstance(node.op, ast.Pow):
                exponent = node.right
                sign = 1
                if isinstance(exponent, ast.UnaryOp):
                    sign = -1 if isinstance(exponent.op, ast.USub) else 1
                    exponent = exponent.operand
                value = exponent.value if isinstance(exponent, ast.Constant) else self.parameters.get(exponent.id) if isinstance(exponent, ast.Name) else None
                if type(value) not in (int, float) or abs(sign * value) > 8:
                    raise ValueError('幂指数须为绝对值不超过8的常数或自定义参数')
            return
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)):
            self.check(node.left, depth + 1); self.check(node.comparators[0], depth + 1)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords and len(node.args) == FUNCTIONS[node.func.id]:
            for arg in node.args:
                self.check(arg, depth + 1)
            return
        raise ValueError('公式包含未知变量或不支持的操作；仅允许数学运算和已列出的函数')

    def evaluate(self, density, v0, rho_max):
        import numpy as np
        values = {**{name: float(value) for name, value in self.parameters.items()}, 'rho': np.asarray(density, dtype=float), 'v0': v0, 'rho_max': rho_max}
        funcs = {'exp': np.exp, 'log': np.log, 'sqrt': np.sqrt, 'abs': np.abs,
                 'min': np.minimum, 'max': np.maximum, 'clip': np.clip, 'where': np.where}
        binary = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide, ast.Pow: np.power}
        compare = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater, ast.GtE: np.greater_equal, ast.Eq: np.equal, ast.NotEq: np.not_equal}
        def calculate(node):
            if isinstance(node, ast.Constant):
                return float(node.value)
            if isinstance(node, ast.Name):
                return values[node.id]
            if isinstance(node, ast.UnaryOp):
                value = calculate(node.operand)
                return -value if isinstance(node.op, ast.USub) else value
            if isinstance(node, ast.BinOp):
                return binary[type(node.op)](calculate(node.left), calculate(node.right))
            if isinstance(node, ast.Compare):
                return compare[type(node.ops[0])](calculate(node.left), calculate(node.comparators[0]))
            return funcs[node.func.id](*(calculate(arg) for arg in node.args))
        try:
            with np.errstate(over='raise', divide='raise', invalid='raise', under='ignore'):
                result = np.asarray(calculate(self.tree))
            if result.dtype.kind not in ('f', 'i', 'u'):
                raise ValueError('公式必须输出数值速度，不能只输出比较条件')
            result = np.broadcast_to(result.astype(float), values['rho'].shape).copy()
            if not np.all(np.isfinite(result)) or np.any(result < -1e-8) or np.any(result > v0 + 1e-8):
                raise ValueError('公式输出速度须为0至v0之间的有限实数，单位m/s')
            return np.clip(result, 0, v0)
        except (FloatingPointError, OverflowError, ZeroDivisionError, TypeError):
            raise ValueError('公式在当前密度下出现除零、溢出或非法数学定义域，请使用 max、clip 等明确处理边界') from None


def normalize_custom_methods(value):
    if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value['items'], list) or len(value['items']) > MAX_METHODS:
        raise ValueError('自定义公式须为items列表，每个任务最多保存6种')
    result, ids, names = [], set(), set()
    for source in value['items']:
        allowed = {'id', 'name', 'kind', 'version', 'expression', 'parameters', 'description', 'boundary', 'source'}
        if not isinstance(source, dict) or set(source) - allowed:
            raise ValueError('自定义公式包含不支持的字段')
        item = copy.deepcopy(source)
        if not isinstance(item.get('id'), str) or not re.fullmatch(r'custom_[a-f0-9]{32}', item['id']) or item['id'] in ids:
            raise ValueError('自定义公式ID无效或重复')
        ids.add(item['id'])
        for key, limit in (('name', 100), ('expression', 1000), ('description', 2000), ('boundary', 1000), ('source', 1000)):
            text = item.get(key, '')
            if not isinstance(text, str) or len(text) > limit or (key in ('name', 'expression') and not text.strip()):
                raise ValueError(f'自定义公式{key}须为不超过{limit}字的有效文本')
            item[key] = text.strip()
        if item['name'] in names:
            raise ValueError('同一任务的自定义公式名称不能重复')
        names.add(item['name'])
        item.setdefault('kind', 'speed_density'); item.setdefault('version', 1); item.setdefault('parameters', [])
        if item['kind'] != 'speed_density' or type(item['version']) is not int or not 1 <= item['version'] <= 1000000:
            raise ValueError('当前支持速度—密度公式，公式版本须为正整数')
        if not isinstance(item['parameters'], list) or len(item['parameters']) > 12:
            raise ValueError('每种公式最多配置12个参数')
        parameters = set()
        for parameter in item['parameters']:
            if not isinstance(parameter, dict) or set(parameter) - {'name', 'value', 'label', 'unit'}:
                raise ValueError('公式参数格式无效')
            name = parameter.get('name')
            if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', name) or name in VARIABLES | set(FUNCTIONS) | parameters:
                raise ValueError('参数名须为小写英文开头的字母数字下划线，最长32位，不能重复或占用内置名称')
            parameters.add(name)
            number = parameter.get('value')
            if type(number) not in (int, float) or abs(number) > 1e6 or not math.isfinite(number):
                raise ValueError('参数值须为-1000000至1000000之间的有限数值')
            for key, limit in (('label', 80), ('unit', 32)):
                if not isinstance(parameter.get(key, ''), str) or len(parameter.get(key, '')) > limit:
                    raise ValueError('参数说明最多80字，单位最多32字')
        SpeedFormula(item['expression'], item['parameters'])
        result.append(item)
    return {'items': result}


def custom_method_metadata(item):
    digest = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'id': item['id'], 'name': item['name'], 'version': item['version'], 'custom': True, 'kind': 'speed_density',
            'description': item['description'] or '用户定义的速度—密度关系，在共用路网上计算人流演化。',
            'equation': 'v(rho) = ' + item['expression'], 'expression': item['expression'], 'parameters': item['parameters'],
            'source': item['source'], 'formula_hash': digest,
            'boundary': (item['boundary'] + '；' if item['boundary'] else '') + '输出为0至v0的速度（m/s）；与其他方法共用人数守恒、容量和通道约束，尚未完成现场标定。'}


def preview_formula(value, v0, rho_max):
    import numpy as np
    for number, low, high in ((v0, .2, 2.5), (rho_max, 2, 10)):
        if type(number) not in (int, float) or not low <= number <= high or not math.isfinite(number):
            raise ValueError('预览自由速度须为0.2至2.5 m/s，拥挤密度须为2至10人/㎡')
    item = normalize_custom_methods({'items': [value]})['items'][0]
    density = np.linspace(0., rho_max, 101)
    speed = SpeedFormula(item['expression'], item['parameters']).evaluate(density, v0, rho_max)
    return {'method': item, 'samples': [{'density': round(float(rho), 5), 'speed': round(float(v), 5)} for rho, v in zip(density, speed)],
            'note': '预览检查当前参数下的101个密度采样点，不代表覆盖所有输入或已完成场景标定；运行时继续检查每个时间步的输出。'}
