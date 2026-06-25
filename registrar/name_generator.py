"""随机英文名生成器（用于 onboarding 的 firstName/lastName + API Key 名称）。

降低风控：用真实感英文名而非写死 "User" / timestamp，更像真人注册。

姓名表来自 Faker en_US person provider（SSA 1960s-1990s Top 200 名 + Census Top 1000 姓），
按真实人口分布加权抽样，避免均匀抽样造成的"批量注册"指纹。
详见 names_data.py（数据源注明）。

API Key 名称采用多模式随机组合，避免所有 Key 都叫 "test" 的明显特征。
"""
from __future__ import annotations

import random
import secrets

from names_data import FIRST_NAMES_FEMALE, FIRST_NAMES_MALE, LAST_NAMES

# 模块级 CSPRNG 实例（SystemRandom 基于 os.urandom，加密安全）。
# secrets 模块不支持 weights 参数，故加权抽样用 SystemRandom().choices。
_RNG = random.SystemRandom()


# 合并男/女名表为单一加权池。
# 男名表与女名表权重总和均归一化为 1.0，合并后总权重 2.0。
# 对 13 个跨性别中性名（Angel/Casey/Taylor/Kelly/Alexis/Kerry/Jamie/Jordan/
# Shannon/Leslie/Tracy/Terry/Jaime），权重求和——语义上"该名总使用频率 = 男频 + 女频"，
# 比覆盖去重更贴近真实人口分布。
_FIRST_NAMES: list[str] = []
_FIRST_WEIGHTS: list[float] = []
_seen_first: set[str] = set()
for _name, _w in (*FIRST_NAMES_MALE.items(), *FIRST_NAMES_FEMALE.items()):
    if _name in _seen_first:
        # 中性名：累加权重到已有条目
        _idx = _FIRST_NAMES.index(_name)
        _FIRST_WEIGHTS[_idx] += _w
    else:
        _seen_first.add(_name)
        _FIRST_NAMES.append(_name)
        _FIRST_WEIGHTS.append(_w)

_LAST_NAMES = list(LAST_NAMES.keys())
_LAST_WEIGHTS = list(LAST_NAMES.values())

# API Key 名称组合词库（多模式，见 gen_key_name）
_KEY_ENVS = ["prod", "dev", "staging", "test", "qa", "sandbox", "local", "beta", "alpha", "preview"]
_KEY_SCOPES = ["api", "app", "web", "cli", "bot", "agent", "service", "core", "main", "side"]
_KEY_NOUNS = ["key", "token", "access", "cred", "auth", "gateway", "client", "worker", "runner", "bridge"]
_KEY_SEP = ["-", "_", "."]


def _weighted_choice(names: list[str], weights: list[float]) -> str:
    """加权随机抽取一个名字（CSPRNG 加权抽样）。"""
    return _RNG.choices(names, weights=weights, k=1)[0]


def gen_first_name() -> str:
    """加权随机抽取 firstName（男/女名合并为单一加权池，中性名权重求和）。

    Faker en_US 的 first_names = first_names_male + first_names_female（OrderedDict），
    权重按 SSA 1960s-1990s 数量加权；两表权重总和均归一化为 1.0，合并后总权重 2.0。
    对跨性别中性名权重求和（见模块顶部 _FIRST_NAMES 构建逻辑），反映真实总使用频率。
    """
    return _weighted_choice(_FIRST_NAMES, _FIRST_WEIGHTS)


def gen_last_name() -> str:
    """加权随机抽取 lastName（Census Top 1000，按出现次数加权）。"""
    return _weighted_choice(_LAST_NAMES, _LAST_WEIGHTS)


def gen_full_name() -> tuple[str, str]:
    """返回 (firstName, lastName)。"""
    return gen_first_name(), gen_last_name()


# Key 名称生成模式（随机选一种，避免单一模式指纹）
def _key_mode_env_noun() -> str:
    """模式1：env-noun-hex（如 prod-key-a3f9）。"""
    sep = secrets.choice(_KEY_SEP)
    suffix = secrets.token_hex(2)
    return f"{secrets.choice(_KEY_ENVS)}{sep}{secrets.choice(_KEY_NOUNS)}{sep}{suffix}"


def _key_mode_scope_noun() -> str:
    """模式2：scope-noun-hex（如 api-client-7b21）。"""
    sep = secrets.choice(_KEY_SEP)
    suffix = secrets.token_hex(2)
    return f"{secrets.choice(_KEY_SCOPES)}{sep}{secrets.choice(_KEY_NOUNS)}{sep}{suffix}"


def _key_mode_name_based() -> str:
    """模式3：基于随机姓名 + 编号（如 smith-42），更像个人开发者命名。"""
    last = _weighted_choice(_LAST_NAMES, _LAST_WEIGHTS).lower()
    num = secrets.randbelow(90) + 10  # 10-99 两位数
    sep = secrets.choice(_KEY_SEP)
    return f"{last}{sep}{num}"


def _key_mode_project_like() -> str:
    """模式4：项目风（如 proj-alpha-3c），模拟真实项目命名。"""
    sep = secrets.choice(_KEY_SEP)
    suffix = secrets.token_hex(3)
    prefix = secrets.choice(["proj", "app", "service", "exp", "demo", "tool"])
    env = secrets.choice(_KEY_ENVS)
    return f"{prefix}{sep}{env}{sep}{suffix}"


def _key_mode_simple_word() -> str:
    """模式5：单词 + 短 hex（如 gateway-9f），简洁不显眼。"""
    sep = secrets.choice(_KEY_SEP)
    suffix = secrets.token_hex(1)
    return f"{secrets.choice(_KEY_NOUNS + _KEY_SCOPES)}{sep}{suffix}"


_KEY_MODES = [
    _key_mode_env_noun,
    _key_mode_scope_noun,
    _key_mode_name_based,
    _key_mode_project_like,
    _key_mode_simple_word,
]


def gen_key_name() -> str:
    """生成随机 API Key 名称。

    随机从 5 种模式中选一种生成，避免所有 Key 都是同一格式。
    示例：prod-key-a3f9 / api-client-7b21 / smith-42 / proj-beta-3c1d2a / gateway-9f
    """
    return secrets.choice(_KEY_MODES)()


if __name__ == "__main__":
    print("=== 姓名样本 ===")
    for _ in range(5):
        f, l = gen_full_name()
        print(f"  {f} {l}")
    print("\n=== Key 名称样本 ===")
    for _ in range(10):
        print(f"  {gen_key_name()}")
