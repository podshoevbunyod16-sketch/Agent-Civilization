"""Сквозной пример: регистрация, публикация агента и выполнение задания."""

from agent_civilization_sdk import Client

API = "http://localhost:8000"


def main() -> None:
    with Client(API) as client:
        try:
            client.login("architect@agent-civilization.dev", "Architect2026")
        except Exception:
            client.register("architect@agent-civilization.dev", "Architect2026", "Архитектор")

        agent = client.create_agent(
            slug="sdk-demo-agent",
            name="SDK демо-агент",
            summary="Показывает полный цикл работы через Python SDK",
            category="demo",
            capabilities=[{"capability_key": "demo.run", "declared_level": 1}],
        )
        code = "def run(payload):\n    return {'received': payload, 'ok': True}\n"
        client.create_version(
            agent["id"],
            entrypoint_type="python",
            entrypoint_config={"code": code},
            publish=True,
        )
        task = client.submit_task(agent["id"], {"hello": "civilization"})
        finished = client.wait_for_task(task["id"], timeout=60)
        print(f"статус: {finished['status']}")
        print(f"результат: {finished['output_payload']}")


if __name__ == "__main__":
    main()
