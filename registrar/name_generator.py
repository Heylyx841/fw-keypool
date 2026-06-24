"""随机英文名生成器（用于 onboarding 的 firstName/lastName + API Key 名称）。

降低风控：用真实感英文名而非写死 "User" / timestamp，更像真人注册。
内置常见英文名库，零依赖。
"""
from __future__ import annotations

import secrets

# 常见英文名（firstName）
_FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph",
    "Thomas", "Charles", "Christopher", "Daniel", "Matthew", "Anthony", "Mark",
    "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Kevin", "Brian",
    "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan",
    "Jessica", "Sarah", "Karen", "Lisa", "Nancy", "Betty", "Margaret", "Sandra",
    "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Carol", "Amanda", "Dorothy",
    "Alex", "Chris", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Quinn",
    "Sam", "Jamie", "Cameron", "Avery", "Parker", "Reese", "Hayden", "Rowan",
]

# 常见英文姓（lastName）
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz", "Parker",
    "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris", "Morales", "Murphy",
]

# API Key 名称形容词 + 名词组合
_KEY_ADJ = ["prod", "dev", "test", "main", "alpha", "beta", "core", "api", "app", "service"]
_KEY_NOUN = ["key", "token", "access", "cred", "auth", "gateway", "client", "agent", "bot", "worker"]


def gen_first_name() -> str:
    return secrets.choice(_FIRST_NAMES)


def gen_last_name() -> str:
    return secrets.choice(_LAST_NAMES)


def gen_full_name() -> tuple[str, str]:
    """返回 (firstName, lastName)。"""
    return gen_first_name(), gen_last_name()


def gen_key_name() -> str:
    """生成随机 API Key 名称（如 prod-key-a3f9）。"""
    suffix = secrets.token_hex(2)  # 4 位十六进制
    return f"{secrets.choice(_KEY_ADJ)}-{secrets.choice(_KEY_NOUN)}-{suffix}"


if __name__ == "__main__":
    for _ in range(3):
        f, l = gen_full_name()
        print(f" name: {f} {l}   key: {gen_key_name()}")
