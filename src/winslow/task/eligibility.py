from winslow import exceptions
from winslow.logger import LOGGER


def check_task_eligibility(task, logger=LOGGER):
    if task._is_eligible_result is not None:
        logger.debug(
            f"Eligibility already resolved for {task} - is_eligible will not be called."
        )
        return task._is_eligible_result

    logger.debug(f"Checking eligibility: {task}")
    try:
        result = task._evaluate_is_eligible()
    except exceptions.TaskSkip as e:
        logger.info(e)
        result = False
    except Exception as e:
        # A crash in is_eligible aborts the run: the error is logged with
        # the full traceback and raised again as an EligibilityError.
        #
        # The crash is not an answer. Example: is_eligible asks a database
        # if the source table holds rows, and the connection fails. A False
        # here would drop the task silently, and its dependents would run
        # without it (see Graph). The abort sends the author to the
        # traceback instead, and the wrap gives the CLI its clean error.
        logger.error(f"is_eligible crashed for {task}", exc_info=True)
        raise exceptions.EligibilityError(f"is_eligible crashed for {task}: {e}") from e
    task._is_eligible_result = result
    return result
