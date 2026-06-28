"""Prefect flows. Phase 0: a smoke-test flow that proves the orchestrator runs."""

from __future__ import annotations

from prefect import flow, task

from api.observability import configure_logging, get_logger


@task(retries=0)
def hello_task(message: str) -> str:
    log = get_logger("prefect.task")
    log.info("hello_task.run", message=message)
    return f"hello, {message}"


@flow(name="hello-world", log_prints=True)
def hello_world_flow(name: str = "world") -> str:
    configure_logging()
    log = get_logger("prefect.flow")
    log.info("hello_world_flow.start", name=name)
    result = hello_task(name)
    log.info("hello_world_flow.done", result=result)
    return result


if __name__ == "__main__":
    print(hello_world_flow())
