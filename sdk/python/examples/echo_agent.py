"""Пример python-агента для Agent Civilization.

Опубликуйте этот файл как версию агента (entrypoint_type="python"):

    from agent_civilization_sdk import Client

    client = Client("http://localhost:8000")
    client.login("dev@example.com", "SuperSecret123")
    agent = client.create_agent(
        slug="echo-agent",
        name="Эхо-агент",
        summary="Возвращает входные данные и статистику текста",
        capabilities=[{"capability_key": "text.analyze", "declared_level": 1}],
    )
    client.create_version(
        agent["id"],
        entrypoint_type="python",
        entrypoint_config={"code": open(__file__).read()},
        publish=True,
    )
    task = client.submit_task(agent["id"], {"text": "привет"})
    print(client.wait_for_task(task["id"])["output_payload"])
"""


def run(payload: dict) -> dict:
    text = str(payload.get("text", ""))
    return {
        "echo": payload,
        "length": len(text),
        "words": len(text.split()),
        "upper": text.upper(),
    }
