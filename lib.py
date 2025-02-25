import asyncio
import functools
import io
import json
import logging
import os
import pathlib
import shutil
import statistics as stats
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Cursor
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import discord
import docker
from discord.ext import commands

from config import settings
from database import Database
from error_handler import get_full_class_name

doc = docker.from_env()

logger = logging.getLogger(__name__)


async def benchmark(ctx: commands.Context, day: int, part: int, code: bytes, ):
    """Run the entire benchmark process, end-to-end."""
    op_name, op_id = ctx.author.name, ctx.author.id

    try:
        db = Database().get()
        cur = db.cursor()
        results = []
        with tempfile.TemporaryDirectory(suffix=f"-ferris-elf-{op_id}") as tmpdir:
            populate_tmp_dir(tmpdir, code)
            if not await build_code(op_name, op_id, tmpdir):
                # This reply is not good UX, but it's better than silence.
                await ctx.reply("Build failed.")
                return
            answers_map = load_answers(cur, day, part)
            for in_file in get_input_files(day):
                logger.info("Processing file: %s", in_file)
                load_input(tmpdir, day, in_file)
                result_lst = await run_code(op_name, op_id, tmpdir, in_file)
                result = process_run_result(in_file, answers_map, result_lst)
                results.append(result)

            if len(results) > 0:
                save_results(cur, op_id, day, part, code, results)
                db.commit()

        verified_results = [r for r in results if r.verified]
        if len(verified_results) > 0:
            median = stats.mean([r.median for r in verified_results])
            average = stats.mean([r.average for r in verified_results])
            await ctx.reply(
                embed=discord.Embed(
                    title="Benchmark complete (Verified)",
                    description=f"Median: **{ns(median)}**\nAverage: **{ns(average)}**",
                )
            )
        else:
            median = stats.mean([r.median for r in results])
            average = stats.mean([r.average for r in results])
            await ctx.reply(
                embed=discord.Embed(
                    title="Benchmark complete (Unverified)",
                    description=f"Median: **{ns(median)}**\nAverage: **{ns(average)}**",
                )
            )

    except Exception as e:
        logger.exception(f"Unhandled exception while benchmarking day {day}, part {part}.")
        await ctx.reply(f"Unhandled exception while benchmarking day {day}, part {part}.")
        # with io.BytesIO() as buf:
        #     buf.write(bytes(''.join(
        #         traceback.format_exception(e)), encoding='utf8'))
        #     buf.seek(0)
        #     errtxt = (f"Unhandled exception while benchmarking day {day}, part {part}: `{get_full_class_name(e)}`: "
        #               f"`{e}`")[:2000]
        #     await ctx.reply(errtxt, file=discord.File(buf, filename="traceback.txt"))


def populate_tmp_dir(tmp_dir: str, solution_code: bytes):
    """
    Set up tmp_dir for building. This copies in the runner and submitted code,
    but not the AOC inputs. We'll read those later.
    """
    logger.info("Building temp dir to use as volume")
    # Step 1: Copy all the rust files
    script_dir = os.path.dirname(__file__)
    runner_src_dir = os.path.join(script_dir, "runner")
    ignores = shutil.ignore_patterns("*target*", "Dockerfile", "**/.gitkeep")
    shutil.copytree(runner_src_dir, tmp_dir, dirs_exist_ok=True, ignore=ignores)

    # Step 2: Write code.
    code_path = os.path.join(tmp_dir, "src", "code.rs")
    with open(code_path, "wb") as code_file:
        code_file.write(solution_code)


async def build_code(author_name: str, author_id: int, tmp_dir: str) -> bool:
    """
    Designed to be used with a basic rust container. Run the container
    with `cargo build` to build the code. Code is mounted in as a volume,
    so that binaries are saved between build/run.
    """
    logger.info("Running container to build code for %s", author_id)
    try:
        loop = asyncio.get_event_loop()
        out = await loop.run_in_executor(None, functools.partial(
            doc.containers.run,
            settings.docker.container_ref,
            "timeout --kill-after=5s 30s cargo build",
            remove=True,
            stdout=True,
            mem_limit="8g",
            # network_mode="none", # Want no-network, but it's downloading crates. :(
            volumes={tmp_dir: {'bind': '/app', 'mode': 'rw'}}
        ))
        out = out.decode("utf-8")
        logger.debug("Build container output: %s", out)
        return True
    except docker.errors.ContainerError:
        logger.exception("Error in docker while building code")
        return False


def load_answers(cursor: Cursor, day: int, part: int):
    """Load the expected answers for each input file."""
    rows = cursor.execute(
        "SELECT key, answer2 FROM solutions WHERE day = ? AND part = ?",
        (day, part),
    )

    answer_map = {}
    for r in rows:
        (key, answer) = r
        answer_map[key] = answer

    return answer_map


def get_input_files(day: int) -> list[str]:
    """
    List all the input files that exist for the current day.
    """
    day_path = get_input_dir_for_day(day)
    return [f for f in os.listdir(day_path) if os.path.isfile(os.path.join(day_path, f))]


def load_input(tmp_dir: str, day: int, file_name: str):
    """
    Populate tmp_dir with the input files for the requested year/day.
    """
    day_path = get_input_dir_for_day(day)
    container_inputs_path = os.path.join(tmp_dir, "inputs")

    # Step 1: Clear out anything there

    for path in pathlib.Path(container_inputs_path).glob("**/*"):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    for path in pathlib.Path(container_inputs_path).glob("**/.*"):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    # Step 2: Copy the appropriate file.
    shutil.copy(os.path.join(day_path, file_name), container_inputs_path)


async def run_code(author_name: str, author_id: int, tmp_dir: str, in_file: str) -> Optional[list[str]]:
    """
    Designed to be used with a basic rust container. Given the code already
    built in tmp_dir as a volume, run the benchmark itself.
    """
    in_file_name = os.path.join("/app", "inputs", in_file)
    logger.info("Running container to run code for %s", author_id)
    try:
        loop = asyncio.get_event_loop()
        out = await loop.run_in_executor(None, functools.partial(
            doc.containers.run,
            settings.docker.container_ref,
            "timeout --kill-after=5s 60s cargo criterion --message-format=json",
            environment={
                "FERRIS_ELF_INPUT_FILE_NAME": in_file_name,
            },
            remove=True,
            stdout=True,
            stderr=True,
            mem_limit="8g",
            # network_mode="none", # Downloading crates.
            volumes={tmp_dir: {'bind': '/app', 'mode': 'rw'}}
        ))
        out: str = out.decode("utf-8")
        logger.debug("Run container output (type: %s):\n%s", type(out), out)
        results = []

        for l in out.splitlines():
            if len(l) == 0 or l[0] != '{':
                continue
            l_data = json.loads(l)
            results.append(l_data)

        logger.debug("Results from container run for user %s, file %s: %s", author_id, in_file, results)
        return results
    except docker.errors.ContainerError:
        logger.exception("Error in docker while running code")
        return None


@dataclass(slots=True)
class RunResult:
    """Dataclass holding summary of a benchmarking run."""
    answer: int | str
    verified: bool
    # These are optional because defaulting to zero seems like a bad idea.
    typical: Optional[float]
    average: Optional[float]
    median: Optional[float]
    high_bound: Optional[float]
    low_bound: Optional[float]


def process_run_result(in_file, answers_map, result_lst) -> RunResult:
    """Given JSON blobs extracted from a container's stdout, get the core stats out."""
    # `result` should be a dataclass, not a dict. Another time.
    result = RunResult(answer="", verified=False, typical=None, average=None, median=None, high_bound=None,
                       low_bound=None)
    for blob in result_lst:
        reason = blob.get("reason", None)
        if reason is None:
            continue
        elif reason == "ferris-answer":
            answer = blob["answer"]
            result.answer = answer
            if answers_map.get(in_file, None) == answer:
                result.verified = True
            else:
                result.verified = False
        elif reason == "benchmark-complete":
            result.typical = blob["typical"]["estimate"]
            result.average = blob["mean"]["estimate"]
            result.median = blob["median"]["estimate"]
            result.high_bound = blob["typical"]["upper_bound"]
            result.low_bound = blob["typical"]["lower_bound"]
    logger.info("Computed run result: %s", result)
    return result


def save_results(cur: Cursor, author_id: int, day: int, part: int, code: bytes, results):
    """
    Save the benchmark run results to the DB.
    """

    str_code = code.decode()
    db_results = [(str(author_id), str_code, day, part, r.median,
                   int(r.answer) if isinstance(r.answer, int) or r.answer.isdigit() else None, r.answer) for r in
                  results]

    query = "INSERT INTO runs(user, code, day, part, time, answer, answer2) VALUES (?, ?, ?, ?, ?, ?, ?)"

    cur.executemany(query, db_results)


def ns(v) -> str:
    """Format number of nanoseconds for display."""
    if v > 1e9:
        return f"{v / 1e9:.2f}s"
    if v > 1e6:
        return f"{v / 1e6:.2f}ms"
    if v > 1e3:
        return f"{v / 1e3:.2f}µs"
    return f"{v:.0f}ns"


def get_best_times(day) -> Tuple[list[Tuple[int, str]], list[Tuple[int, str]]]:
    """
    Get the current contents of the leaderboard for the given day. Results are returned as a 
    tuple of lists, first for Part 1, then for Part 2. Each list is of (user_id, formatted_time).
    """
    db = Database().get()
    query = """SELECT user, MIN(time) FROM runs WHERE day = ? AND part = ?
           GROUP BY user ORDER BY time"""

    times1 = []
    for user_id, time in db.cursor().execute(query, (day, 1)):
        if user_id is None or time is None:
            continue
        user_id = int(user_id)
        times1.append((user_id, ns(time)))
    times2 = []
    for user_id, time in db.cursor().execute(query, (day, 2)):
        if user_id is None or time is None:
            continue
        user_id = int(user_id)
        times2.append((user_id, ns(time)))
    return (times1, times2)


def get_input_dir_for_day(day: int) -> str:
    """Return the exact directory that contains the input files for the given day."""
    day_path = os.path.join(settings.aoc.inputs_dir, str(day))
    return os.path.abspath(day_path)


def year() -> int:
    """Return the current year, as AOC code should understand it."""
    stamp = datetime.now(tz=ZoneInfo("America/New_York"))
    return stamp.year


def today() -> int:
    """Return the current day, as AOC code should understand it."""
    stamp = datetime.now(tz=ZoneInfo("America/New_York"))
    return min(stamp.day, 25)
