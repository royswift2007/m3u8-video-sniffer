"""
Download manager for task queue management and execution.
"""

from queue import Empty, Queue
import threading
import time
from datetime import datetime
from typing import Callable, List
from urllib.parse import urlparse

from core.task_model import DownloadTask
from core.engine_selector import EngineSelector
from engines.base_engine import BaseEngine
from utils.logger import logger
from utils.notification import (
    notify_download_started,
    notify_download_completed,
    notify_download_failed,
)


class DownloadManager:
    """Download task manager."""

    def __init__(self, engines: list[BaseEngine], max_concurrent: int = 3):
        self.engines = engines
        self.selector = EngineSelector(engines)
        self.max_concurrent = max_concurrent
        self.task_queue = Queue()
        self.active_tasks: List[DownloadTask] = []
        self.paused_tasks: List[DownloadTask] = []
        self.completed_tasks: List[DownloadTask] = []
        self.failed_tasks: List[DownloadTask] = []
        self._workers = []
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()
        self.on_task_update: Callable | None = None
        self._metrics = {
            "success_total": 0,
            "failed_total": 0,
            "by_engine": {},
            "by_stage": {},
        }
        self._start_workers()

    def add_task(self, task: DownloadTask, user_engine_preference: str = None):
        """Add a download task into queue."""
        with self._lock:
            if task in self.active_tasks:
                logger.info(f"任务正在执行中，跳过重复入队: {task.filename}")
                return
            self._remove_task_from_state_lists(task)

        if self._is_task_queued(task):
            logger.info(f"任务已在队列中，跳过重复入队: {task.filename}")
            return

        engine, engine_name = self.selector.select(task.url, user_engine_preference)
        self._reset_task_runtime(task)
        task.engine = engine_name
        task.status = "waiting"

        user_specified = user_engine_preference is not None
        self.task_queue.put((task, engine, user_specified))
        logger.info(
            f"任务已加入队列: {task.filename} (引擎: {engine_name}, 用户指定: {user_specified})"
        )

        if self.on_task_update:
            self.on_task_update(task)

    def _reset_task_runtime(self, task: DownloadTask):
        """Reset task runtime fields before queueing."""
        task.error_message = ""
        task.stop_requested = False
        task.stop_reason = ""
        task.speed = ""
        task.downloaded_size = ""
        task.retry_count = 0
        task.started_at = None
        task.completed_at = None
        task.process = None
        task.progress = 0.0
        setattr(task, "_history_recorded_status", None)

    def _remove_task_from_state_lists(self, task: DownloadTask):
        """Remove task from all in-memory state lists (dedup-safe)."""
        for bucket in (
            self.active_tasks,
            self.paused_tasks,
            self.completed_tasks,
            self.failed_tasks,
        ):
            while task in bucket:
                bucket.remove(task)

    def _is_task_queued(self, task: DownloadTask) -> bool:
        """Check if task already exists in queue."""
        with self.task_queue.mutex:
            return any(entry[0] is task for entry in self.task_queue.queue)

    def _snapshot_queued_tasks(self) -> list[DownloadTask]:
        """Thread-safe queued task snapshot."""
        with self.task_queue.mutex:
            return [entry[0] for entry in list(self.task_queue.queue)]

    def _remove_task_from_queue(self, task: DownloadTask) -> int:
        """Remove queued entries matching task and fix queue counters."""
        removed = 0
        with self.task_queue.mutex:
            old_entries = list(self.task_queue.queue)
            kept_entries = []
            for entry in old_entries:
                if entry[0] is task:
                    removed += 1
                else:
                    kept_entries.append(entry)
            if removed:
                self.task_queue.queue.clear()
                self.task_queue.queue.extend(kept_entries)
                self.task_queue.unfinished_tasks = max(
                    0, self.task_queue.unfinished_tasks - removed
                )
                if self.task_queue.unfinished_tasks == 0:
                    self.task_queue.all_tasks_done.notify_all()
                self.task_queue.not_full.notify_all()
        return removed

    @staticmethod
    def _unique_tasks(tasks: list[DownloadTask]) -> list[DownloadTask]:
        """Deduplicate tasks by object identity while keeping order."""
        seen = set()
        result = []
        for task in tasks:
            marker = id(task)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(task)
        return result

    def _start_workers(self):
        """Start worker threads."""
        for i in range(self.max_concurrent):
            worker = threading.Thread(
                target=self._worker,
                name=f"DownloadWorker-{i + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        logger.info(f"已启动 {self.max_concurrent} 个下载工作线程")

    def set_max_concurrent(self, new_value: int):
        """Dynamically adjust concurrent worker count."""
        old_value = self.max_concurrent
        self.max_concurrent = new_value

        if new_value > old_value:
            for i in range(old_value, new_value):
                worker = threading.Thread(
                    target=self._worker,
                    name=f"DownloadWorker-{i + 1}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
            logger.info(f"并发数调整: {old_value} -> {new_value}，已启动新工作线程")
        else:
            logger.info(f"并发数调整: {old_value} -> {new_value}")

    def _classify_failure(self, message: str) -> str:
        """Roughly classify failure reason."""
        if not message:
            return "unknown"
        text = message.lower()
        if "用户取消" in text or "用户暂停" in text or "cancelled" in text or "paused" in text:
            return "stopped"
        if "401" in text or "403" in text or "forbidden" in text or "unauthorized" in text:
            return "auth"
        if "signature" in text or "nsig" in text or "parse" in text or "no video formats" in text:
            return "parse"
        if "timeout" in text or "timed out" in text or "connection reset" in text:
            return "timeout"
        if "usage information" in text or "--help" in text or "unknown option" in text:
            return "parse"
        return "unknown"

    def _detect_failure_stage(self, message: str) -> str:
        """Infer rough failure stage for observability."""
        if not message:
            return "unknown"
        text = message.lower()

        if "cancelled" in text or "paused" in text or "用户取消" in text or "用户暂停" in text:
            return "stopped"
        if "401" in text or "403" in text or "forbidden" in text or "unauthorized" in text:
            return "auth"
        if (
            "m3u8" in text
            or "master playlist" in text
            or "media playlist" in text
            or "manifest" in text
        ):
            return "playlist"
        if "ext-x-key" in text or "enc.key" in text or "decrypt" in text:
            return "key"
        if ".ts" in text or "segment" in text or "fragment" in text or "chunk" in text:
            return "segment"
        if "mux" in text or "merge" in text or "ffmpeg" in text:
            return "merge"
        return "unknown"

    def _is_task_stop_requested(self, task: DownloadTask) -> bool:
        """Return True when task should stop retrying immediately."""
        return self._stop_flag.is_set() or bool(getattr(task, "stop_requested", False))

    def _apply_site_rules_to_task(self, task: DownloadTask) -> bool:
        """Fill missing auth headers from site_rules config."""
        from utils.config_manager import config

        site_rules = config.get("site_rules", []) or []
        url_lower = (task.url or "").lower()
        changed = False

        for rule in site_rules:
            domains = [d.lower() for d in rule.get("domains", [])]
            if not domains:
                continue
            if any(d in url_lower for d in domains):
                referer = rule.get("referer")
                user_agent = rule.get("user_agent")
                headers = rule.get("headers", {}) or {}

                if referer and not task.headers.get("referer"):
                    task.headers["referer"] = referer
                    changed = True
                if user_agent and not task.headers.get("user-agent"):
                    task.headers["user-agent"] = user_agent
                    changed = True
                for k, v in headers.items():
                    if k and v and not task.headers.get(k):
                        task.headers[k] = v
                        changed = True
                if changed:
                    logger.info("已按站点规则补全请求头", event="download_auth_headers", url=task.url)
                return changed

        return changed

    def _score_m3u8_candidate(self, url: str, task: DownloadTask) -> int:
        """Heuristic score for pre-download candidate ranking."""
        score = 0
        url_lower = (url or "").lower()
        headers = getattr(task, "headers", {}) or {}

        if url_lower.startswith("https://"):
            score += 20
        if ".m3u8" in url_lower:
            score += 40
        if any(k in url_lower for k in ("/hls/", "playlist", "index.m3u8", "media.m3u8")):
            score += 20
        if "master.m3u8" in url_lower:
            score -= 5
        if any(k in url_lower for k in ("ad", "ads", "promo", "tracker")):
            score -= 25

        if headers.get("referer"):
            score += 15
        if headers.get("origin"):
            score += 8
        if headers.get("cookie"):
            score += 25
        if headers.get("authorization"):
            score += 10

        try:
            host = urlparse(url).hostname or ""
            page_host = urlparse(headers.get("referer", "")).hostname or ""
            if host and page_host and host == page_host:
                score += 8
        except Exception:
            pass

        return score

    def _rank_task_candidates(self, task: DownloadTask):
        """Rank task URL candidates and pick the best as primary URL."""
        candidates = []
        seen = set()
        for candidate in [task.url, getattr(task, "media_url", None), getattr(task, "master_url", None)]:
            if not candidate:
                continue
            value = candidate.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            score = self._score_m3u8_candidate(value, task)
            candidates.append((value, score))

        if len(candidates) < 2:
            return

        candidates.sort(key=lambda x: x[1], reverse=True)
        best_url = candidates[0][0]
        if best_url != task.url:
            logger.info(
                "[RANK] 优选下载候选链接",
                event="download_candidate_rank",
                best=best_url,
                ranked=" | ".join([f"{u} ({s})" for u, s in candidates]),
            )
            task.url = best_url
        setattr(task, "candidate_scores", {u: s for u, s in candidates})

    def _record_metric(self, engine: str, stage: str, success: bool):
        """Update aggregated runtime metrics for observability."""
        engine = engine or "unknown"
        stage = stage or "unknown"
        with self._lock:
            if success:
                self._metrics["success_total"] += 1
            else:
                self._metrics["failed_total"] += 1

            by_engine = self._metrics["by_engine"]
            if engine not in by_engine:
                by_engine[engine] = {"success": 0, "failed": 0}
            if success:
                by_engine[engine]["success"] += 1
            else:
                by_engine[engine]["failed"] += 1

            by_stage = self._metrics["by_stage"]
            if stage not in by_stage:
                by_stage[stage] = {"success": 0, "failed": 0}
            if success:
                by_stage[stage]["success"] += 1
            else:
                by_stage[stage]["failed"] += 1

            snapshot = {
                "success_total": self._metrics["success_total"],
                "failed_total": self._metrics["failed_total"],
            }
        logger.info(
            "[METRICS] 下载指标更新",
            event="download_metrics_snapshot",
            engine=engine,
            stage=stage,
            success=success,
            snapshot=snapshot,
        )

    def _learn_site_rule_from_task(self, task: DownloadTask):
        """Learn stable site rule from successful task (opt-in)."""
        from utils.config_manager import config

        if not config.get("site_rules_auto.enabled", False):
            return

        url = (task.url or "").strip()
        headers = task.headers or {}
        referer = headers.get("referer")
        user_agent = headers.get("user-agent")
        origin = headers.get("origin")
        cookie = headers.get("cookie")

        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if not host:
            return
        if not referer and not user_agent:
            return

        site_rules = config.get("site_rules", []) or []
        max_rules = int(config.get("site_rules_auto.max_rules", 50))
        allow_cookie = bool(config.get("site_rules_auto.allow_cookie", False))
        rule_name = f"auto:{host}"

        existing = None
        for rule in site_rules:
            if rule.get("name") == rule_name:
                existing = rule
                break

        rule_headers = {}
        if origin:
            rule_headers["origin"] = origin
        if allow_cookie and cookie:
            rule_headers["cookie"] = cookie

        if existing:
            changed = False
            domains = existing.get("domains", []) or []
            if host not in domains:
                domains.append(host)
                existing["domains"] = domains
                changed = True
            if referer and not existing.get("referer"):
                existing["referer"] = referer
                changed = True
            if user_agent and not existing.get("user_agent"):
                existing["user_agent"] = user_agent
                changed = True
            existing_headers = existing.get("headers", {}) or {}
            for k, v in rule_headers.items():
                if k not in existing_headers:
                    existing_headers[k] = v
                    changed = True
            if changed:
                existing["headers"] = existing_headers
                config.config["site_rules"] = site_rules
                config.save()
                logger.info(
                    "[AUTO-RULE] 更新站点规则",
                    event="site_rule_auto_learned",
                    host=host,
                    rule=rule_name,
                )
            return

        if len(site_rules) >= max_rules:
            logger.warning(
                "[AUTO-RULE] 规则数量已达上限，跳过学习",
                event="site_rule_auto_skipped",
                host=host,
                reason="max_rules_reached",
                max_rules=max_rules,
            )
            return

        new_rule = {
            "name": rule_name,
            "domains": [host],
            "url_keywords": ["m3u8"],
            "referer": referer or "",
            "user_agent": user_agent or "",
            "headers": rule_headers,
            "auto": True,
        }
        site_rules.append(new_rule)
        config.config["site_rules"] = site_rules
        config.save()
        logger.info(
            "[AUTO-RULE] 新增站点规则",
            event="site_rule_auto_learned",
            host=host,
            rule=rule_name,
        )

    def _worker(self):
        """Worker thread loop."""
        while not self._stop_flag.is_set():
            got_task = False
            try:
                with self._lock:
                    active_count = len(self.active_tasks)

                if active_count >= self.max_concurrent:
                    time.sleep(0.5)
                    continue

                try:
                    task, engine, user_specified = self.task_queue.get(timeout=1.0)
                except Empty:
                    continue
                got_task = True

                try:
                    self._execute_download(task, engine, user_specified)
                finally:
                    self.task_queue.task_done()
                    got_task = False
            except Exception as e:
                logger.error(f"工作线程异常: {e}")
                if got_task:
                    # Defensive fallback: ensure queue counter does not leak.
                    try:
                        self.task_queue.task_done()
                    except Exception:
                        pass

    def _execute_download(self, task: DownloadTask, engine: BaseEngine, user_specified: bool = False):
        """Execute one download task with retry/fallback."""
        from utils.config_manager import config

        if self._is_task_stop_requested(task):
            logger.info(f"[SKIP] 任务已被停止，跳过执行: {task.filename}")
            return

        task.status = "downloading"
        task.started_at = datetime.now()
        task.retry_count = 0
        task.max_retries = int(config.get("max_retry_attempts", 2))
        backoff_seconds = int(config.get("retry_backoff_seconds", 1))
        features = config.get("features", {}) or {}
        retry_enabled = features.get("download_retry_enabled", True)
        fallback_enabled = features.get("download_engine_fallback", True)
        hls_probe_enabled = features.get("hls_probe_enabled", True)
        hls_probe_hard_fail = features.get("hls_probe_hard_fail", True)
        ranking_enabled = features.get("download_candidate_ranking_enabled", True)
        auth_retry_first = features.get("download_auth_retry_first", True)
        try:
            auth_retry_per_engine = int(features.get("download_auth_retry_per_engine", 1))
        except (TypeError, ValueError):
            auth_retry_per_engine = 1
        auth_retry_per_engine = max(auth_retry_per_engine, 0)

        with self._lock:
            self._remove_task_from_state_lists(task)
            self.active_tasks.append(task)

        if self.on_task_update:
            self.on_task_update(task)

        if ranking_enabled and ".m3u8" in (task.url or "").lower():
            self._rank_task_candidates(task)

        # Optional m3u8 preflight probe (playlist -> key -> segment)
        if hls_probe_enabled and ".m3u8" in (task.url or "").lower():
            try:
                from core.services.hls_probe import HLSProbe

                probe_result = HLSProbe.probe(task.url, task.headers)
                probe_stage = probe_result.get("stage", "unknown")
                setattr(task, "probe_stage", probe_stage)

                if probe_result.get("ok"):
                    logger.info(
                        "[HLS-PROBE] 预探测通过",
                        event="hls_probe_ok",
                        url=task.url,
                        stage=probe_stage,
                        playlist=probe_result.get("playlist_url"),
                    )
                else:
                    probe_error = probe_result.get("error", "unknown")
                    task.error_message = f"HLS probe failed at {probe_stage}: {probe_error}"
                    logger.warning(
                        "[HLS-PROBE] 预探测失败",
                        event="hls_probe_failed",
                        url=task.url,
                        stage=probe_stage,
                        error=probe_error,
                    )

                    if hls_probe_hard_fail and not self._is_task_stop_requested(task):
                        task.status = "failed"
                        with self._lock:
                            self._remove_task_from_state_lists(task)
                            self.failed_tasks.append(task)
                        notify_download_failed(task.filename, task.error_message)
                        logger.error(
                            f"[FAILED] 任务失败: {task.filename}",
                            event="download_failed",
                            engine=task.engine,
                            url=task.url,
                            failure_kind="probe",
                            stage=probe_stage,
                        )
                        with self._lock:
                            while task in self.active_tasks:
                                self.active_tasks.remove(task)
                        if self.on_task_update:
                            self.on_task_update(task)
                        return
            except Exception as e:
                logger.warning(
                    "[HLS-PROBE] 预探测执行异常，继续下载流程",
                    event="hls_probe_exception",
                    url=task.url,
                    error=str(e),
                )

        notify_download_started(task.filename, task.engine)

        def progress_callback(data: dict):
            try:
                task.progress = data.get("progress", task.progress)
                task.speed = data.get("speed", "")
                task.downloaded_size = data.get("downloaded", "")
                if self.on_task_update:
                    self.on_task_update(task)
            except Exception as e:
                logger.debug(f"进度更新异常（可忽略）: {e}")

        def _try_download(selected_engine: BaseEngine, engine_name: str) -> bool:
            if self._is_task_stop_requested(task):
                return False
            try:
                task.engine = engine_name
                return selected_engine.download(task, progress_callback)
            except Exception as e:
                task.error_message = str(e)
                logger.error(
                    f"[FAILED] 任务异常: {task.filename} - {e}",
                    event="download_engine_exception",
                    engine=engine_name,
                    url=task.url,
                    stage="engine_invoke",
                )
                return False

        candidates = self.selector.get_candidates(task.url)
        if user_specified:
            preferred = self.selector.get_engine_by_name(task.engine)
            if preferred:
                candidates = [(preferred, task.engine)] + [c for c in candidates if c[0] != preferred]

        success = False
        last_failure_kind = "unknown"
        last_failure_stage = "unknown"

        while task.retry_count <= task.max_retries and not success:
            if self._is_task_stop_requested(task):
                break

            last_error_message = ""
            last_failure_kind = "unknown"
            last_failure_stage = "unknown"
            candidate_list = candidates if fallback_enabled else candidates[:1]

            for candidate_engine, candidate_name in candidate_list:
                if self._is_task_stop_requested(task):
                    break

                logger.info(
                    f"[TRY] 引擎: {candidate_name}，尝试次数: {task.retry_count + 1}/{task.max_retries + 1}"
                )
                success = _try_download(candidate_engine, candidate_name)
                if success:
                    break

                if self._is_task_stop_requested(task):
                    last_failure_kind = "stopped"
                    last_failure_stage = "stopped"
                    last_error_message = task.error_message or ""
                    break

                last_error_message = task.error_message or ""
                last_failure_kind = self._classify_failure(last_error_message)
                last_failure_stage = self._detect_failure_stage(last_error_message)
                logger.warning(
                    f"[RETRY] 失败类型: {last_failure_kind}",
                    event="download_retry",
                    engine=candidate_name,
                    url=task.url,
                    stage=last_failure_stage,
                )

                if last_failure_kind == "auth":
                    self._apply_site_rules_to_task(task)
                    if auth_retry_first and auth_retry_per_engine > 0:
                        for auth_try in range(auth_retry_per_engine):
                            if self._is_task_stop_requested(task):
                                last_failure_kind = "stopped"
                                last_failure_stage = "stopped"
                                break

                            logger.info(
                                (
                                    f"[AUTH-RETRY] 同引擎重试: {candidate_name} "
                                    f"({auth_try + 1}/{auth_retry_per_engine})"
                                ),
                                event="download_auth_retry",
                                engine=candidate_name,
                                url=task.url,
                                stage="auth",
                            )
                            success = _try_download(candidate_engine, candidate_name)
                            if success:
                                break

                            last_error_message = task.error_message or ""
                            last_failure_kind = self._classify_failure(last_error_message)
                            last_failure_stage = self._detect_failure_stage(last_error_message)
                            logger.warning(
                                "[AUTH-RETRY] 同引擎重试失败",
                                event="download_auth_retry_failed",
                                engine=candidate_name,
                                url=task.url,
                                stage=last_failure_stage,
                                failure_kind=last_failure_kind,
                            )
                            if last_failure_kind != "auth":
                                break

                    if success:
                        break

                    if self._is_task_stop_requested(task):
                        break

                if last_failure_kind == "parse" and fallback_enabled:
                    continue

            if not success:
                if self._is_task_stop_requested(task):
                    break
                task.retry_count += 1
                if task.retry_count > task.max_retries or not retry_enabled:
                    break

                effective_backoff = backoff_seconds
                if last_failure_kind == "timeout":
                    effective_backoff = max(
                        backoff_seconds * (2 ** (task.retry_count - 1)),
                        backoff_seconds,
                    )

                task.status = "waiting"
                if self.on_task_update:
                    self.on_task_update(task)
                if effective_backoff > 0:
                    self._stop_flag.wait(timeout=effective_backoff)

        stop_reason = getattr(task, "stop_reason", "")
        if stop_reason == "removed":
            task.status = "removed"
            task.process = None
            with self._lock:
                self._remove_task_from_state_lists(task)
            logger.info(f"[REMOVED] 任务已移除: {task.filename}")
        elif success:
            task.status = "completed"
            task.progress = 100.0
            task.completed_at = datetime.now()
            task.process = None
            self._learn_site_rule_from_task(task)
            self._record_metric(task.engine, "completed", True)
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.completed_tasks.append(task)
            notify_download_completed(task.filename)
            logger.info(f"[OK] 任务完成: {task.filename}")
        else:
            task.process = None
            if stop_reason == "paused":
                task.status = "paused"
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.paused_tasks.append(task)
                logger.info(f"[PAUSED] 任务已暂停: {task.filename}")
            elif stop_reason == "cancelled":
                task.status = "failed"
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.failed_tasks.append(task)
                self._record_metric(task.engine, "cancelled", False)
                logger.info(f"[CANCELLED] 任务已取消: {task.filename}")
            elif stop_reason == "shutdown":
                self._record_metric(task.engine, "shutdown", False)
                logger.info(f"[STOP] 应用关闭，终止任务: {task.filename}")
            else:
                task.status = "failed"
                with self._lock:
                    self._remove_task_from_state_lists(task)
                    self.failed_tasks.append(task)
                self._record_metric(task.engine, last_failure_stage, False)
                notify_download_failed(task.filename, task.error_message or "所有引擎均失败")
                logger.error(
                    f"[FAILED] 任务失败: {task.filename}",
                    event="download_failed",
                    engine=task.engine,
                    url=task.url,
                    failure_kind=last_failure_kind,
                    stage=last_failure_stage,
                )

        with self._lock:
            while task in self.active_tasks:
                self.active_tasks.remove(task)
        if self.on_task_update:
            self.on_task_update(task)

    def pause_task(self, task: DownloadTask):
        """Pause task."""
        task.stop_requested = True
        task.stop_reason = "paused"
        task.error_message = "用户暂停"
        removed_from_queue = self._remove_task_from_queue(task)
        if removed_from_queue > 0:
            logger.info(f"任务已从等待队列移除并暂停: {task.filename}")
        if task.process:
            try:
                self._kill_process_tree(task.process)
                task.status = "paused"
                logger.info(f"任务已暂停: {task.filename}")
            except Exception as e:
                logger.error(f"暂停任务失败: {e}")
        if task.status in {"waiting", "paused"}:
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.paused_tasks.append(task)
            task.status = "paused"
        if self.on_task_update:
            self.on_task_update(task)

    def resume_task(self, task: DownloadTask):
        """Resume task."""
        logger.info(f"正在继续任务: {task.filename}")
        with self._lock:
            self._remove_task_from_state_lists(task)
        self.add_task(task, task.engine or None)

    def cancel_task(self, task: DownloadTask):
        """Cancel task."""
        task.stop_requested = True
        task.stop_reason = "cancelled"
        task.error_message = "用户取消"
        task.status = "failed"
        removed_from_queue = self._remove_task_from_queue(task)
        if removed_from_queue > 0:
            logger.info(f"任务已从等待队列移除并取消: {task.filename}")
        if task.process:
            try:
                self._kill_process_tree(task.process)
                logger.info(f"任务已取消: {task.filename}")
            except Exception as e:
                logger.error(f"取消任务失败: {e}")
        if task.status in {"waiting", "paused", "failed"} and task not in self.active_tasks:
            with self._lock:
                self._remove_task_from_state_lists(task)
                self.failed_tasks.append(task)
        if self.on_task_update:
            self.on_task_update(task)

    def remove_task(self, task: DownloadTask):
        """Remove task from manager."""
        task.stop_requested = True
        task.stop_reason = "removed"
        task.error_message = "用户删除任务"
        if task.process:
            try:
                self._kill_process_tree(task.process)
            except Exception as e:
                logger.error(f"删除任务时终止进程失败: {e}")
        task.status = "removed"
        self._remove_task_from_queue(task)
        with self._lock:
            self._remove_task_from_state_lists(task)
        logger.info(f"任务已从管理器移除: {task.filename}")

    def _kill_process_tree(self, process):
        """Try to terminate process tree."""
        import os
        import subprocess

        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                logger.debug(f"已使用 taskkill 终止进程: {process.pid}")
                return
            except Exception as e:
                logger.warning(f"taskkill 失败: {e}")

        try:
            import psutil

            proc = psutil.Process(process.pid)
            children = proc.children(recursive=True)
            for child in children:
                child.kill()
            proc.kill()
            logger.debug(
                f"已使用 psutil 终止进程树(PID: {process.pid}, 子进程: {len(children)})"
            )
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def get_all_tasks(self) -> List[DownloadTask]:
        """Return queued + active + completed + failed tasks."""
        queued_tasks = self._snapshot_queued_tasks()
        with self._lock:
            merged = (
                queued_tasks
                + list(self.active_tasks)
                + list(self.paused_tasks)
                + list(self.completed_tasks)
                + list(self.failed_tasks)
            )
        return self._unique_tasks(merged)

    def get_stats(self) -> dict:
        """Return task statistics."""
        queued_tasks = self._unique_tasks(self._snapshot_queued_tasks())
        with self._lock:
            active_tasks = self._unique_tasks(list(self.active_tasks))
            paused_tasks = self._unique_tasks(list(self.paused_tasks))
            completed_tasks = self._unique_tasks(list(self.completed_tasks))
            failed_tasks = self._unique_tasks(list(self.failed_tasks))
        return {
            "queued": len(queued_tasks),
            "active": len(active_tasks),
            "paused": len(paused_tasks),
            "completed": len(completed_tasks),
            "failed": len(failed_tasks),
            "total": (
                len(queued_tasks)
                + len(active_tasks)
                + len(paused_tasks)
                + len(completed_tasks)
                + len(failed_tasks)
            ),
        }

    def get_quality_metrics(self) -> dict:
        """Return aggregated success/failure metrics by engine and stage."""
        with self._lock:
            return {
                "success_total": self._metrics["success_total"],
                "failed_total": self._metrics["failed_total"],
                "by_engine": dict(self._metrics["by_engine"]),
                "by_stage": dict(self._metrics["by_stage"]),
            }

    def shutdown(self):
        """Shutdown download manager and workers."""
        logger.info("正在关闭下载管理器...")
        self._stop_flag.set()

        # Mark and cancel active tasks first.
        for task in list(self.active_tasks):
            task.stop_requested = True
            task.stop_reason = "shutdown"
            if task.process:
                try:
                    self._kill_process_tree(task.process)
                except Exception:
                    pass

        # Drain waiting queue to avoid lingering unfinished tasks.
        drained_tasks = []
        with self.task_queue.mutex:
            while self.task_queue.queue:
                entry = self.task_queue.queue.popleft()
                drained_tasks.append(entry[0])
            if drained_tasks:
                self.task_queue.unfinished_tasks = max(
                    0, self.task_queue.unfinished_tasks - len(drained_tasks)
                )
            if self.task_queue.unfinished_tasks == 0:
                self.task_queue.all_tasks_done.notify_all()
            self.task_queue.not_full.notify_all()

        for task in drained_tasks:
            task.stop_requested = True
            task.stop_reason = "shutdown"
            if task.status == "waiting":
                task.status = "failed"
            with self._lock:
                self._remove_task_from_state_lists(task)

        for worker in self._workers:
            worker.join(timeout=3.0)
        self._workers = []

        logger.info("下载管理器已关闭")
