"""
pattern_miner — 自进化模式挖掘

从 trajectories 表中发现高频重复的工作流级操作模式。
三层过滤管线：基础频率 → 通用模式熵过滤 → 参数结构聚类。

设计要点：
- 增量挖掘：通过 since_traj_id 游标控制范围
- 排除感知工具：read_file / ls / grep_search / get_skeleton 不参与匹配
- 跨会话要求：同一会话内重复不计入
- 已合成排除：通过 exclude_hashes 避免重复合成
"""
import hashlib
import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("PatternMiner")

# 纯感知工具（不参与模式匹配）
PERCEPTION_TOOLS = frozenset({
    "read_file", "ls", "grep_search", "get_skeleton", "recall_step",
})


class PatternMiner:
    """频繁子序列挖掘器。"""

    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self._db = None

    def _get_db(self):
        """延迟获取 SQLite 连接。"""
        if self._db is None:
            from core.memory_db import _get_db
            self._db = _get_db(self.project_dir)
        return self._db

    def mine(
        self,
        min_freq: int = 3,
        min_len: int = 3,
        since_traj_id: Optional[str] = None,
        window_size: int = 50,
        exclude_hashes: Optional[Set[str]] = None,
        universality_threshold: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """从轨迹列表中提取频繁工作流模式。

        参数:
            min_freq: 最小出现频率（跨会话）
            min_len: 最小子序列长度
            since_traj_id: 增量游标
            window_size: 增量窗口大小
            exclude_hashes: 已合成的 pattern hash 集合
            universality_threshold: 通用模式阈值

        返回:
            [{"pattern": [...], "pattern_hash": str, "frequency": int,
              "sessions": [...], "task_summaries": [...], "avg_total_steps": float}]
        """
        exclude_hashes = exclude_hashes or set()

        # 加载轨迹
        trajectories = self._load_trajectories(since_traj_id, window_size)
        if not trajectories:
            logger.debug("无可用轨迹，跳过挖掘")
            return []

        total_count = len(trajectories)
        logger.info(f"开始模式挖掘: {total_count} 条轨迹")

        # 层 1: 基础频率过滤
        candidates = self._layer1_frequency(trajectories, min_freq, min_len)
        logger.debug(f"层 1 过滤后: {len(candidates)} 个候选")

        # 层 2: 通用模式熵过滤
        candidates = self._layer2_universality(candidates, total_count, universality_threshold)
        logger.debug(f"层 2 过滤后: {len(candidates)} 个候选")

        # 排除已合成
        candidates = [c for c in candidates if c["pattern_hash"] not in exclude_hashes]
        logger.debug(f"排除已合成后: {len(candidates)} 个候选")

        # 层 3: 参数结构聚类
        candidates = self._layer3_args_clustering(candidates)
        logger.debug(f"层 3 聚类后: {len(candidates)} 个候选")

        # 二次频率过滤（聚类后子组可能低于阈值）
        candidates = [c for c in candidates if c["frequency"] >= min_freq]

        logger.info(f"挖掘完成: {len(candidates)} 个有效模式")
        return candidates

    # ═══════════════════════════════════════════════════
    # 层 1: 基础频率过滤
    # ═══════════════════════════════════════════════════

    def _layer1_frequency(
        self,
        trajectories: List[Dict],
        min_freq: int,
        min_len: int,
    ) -> List[Dict[str, Any]]:
        """提取所有频率 ≥ min_freq 且跨 ≥ 3 会话的子序列。"""
        # 提取 tool name 序列（排除感知工具）
        traj_sequences = []
        for traj in trajectories:
            tool_calls = json.loads(traj["tool_calls"]) if isinstance(traj["tool_calls"], str) else traj["tool_calls"]
            seq = [tc["name"] for tc in tool_calls if tc["name"] not in PERCEPTION_TOOLS]
            traj_sequences.append({
                "sequence": seq,
                "session_id": traj["session_id"],
                "task_summary": traj.get("task_summary", ""),
                "step_count": traj.get("step_count", len(seq)),
                "tool_calls": tool_calls,
            })

        # 提取所有长度 ≥ min_len 的连续子序列
        pattern_map: Dict[str, Dict] = defaultdict(lambda: {
            "sessions": set(), "count": 0, "task_summaries": [],
            "total_steps": 0, "instances": [],
        })

        for traj_info in traj_sequences:
            seq = traj_info["sequence"]
            seen_in_this_traj = set()  # 同一轨迹中相同子序列只计一次

            for length in range(min_len, len(seq) + 1):
                for start in range(len(seq) - length + 1):
                    subseq = tuple(seq[start:start + length])
                    subseq_key = "|".join(subseq)

                    if subseq_key in seen_in_this_traj:
                        continue
                    seen_in_this_traj.add(subseq_key)

                    info = pattern_map[subseq_key]
                    info["sessions"].add(traj_info["session_id"])
                    info["count"] += 1
                    info["task_summaries"].append(traj_info["task_summary"])
                    info["total_steps"] += traj_info["step_count"]
                    # 保存对应的 tool_calls 片段（用于层 3 参数聚类）
                    tool_calls = traj_info["tool_calls"]
                    # 找到子序列在原始 tool_calls 中的位置
                    instance_calls = self._extract_instance_calls(tool_calls, subseq, start)
                    info["instances"].append(instance_calls)

        # 过滤：频率 ≥ min_freq 且 ≥ 3 个不同会话
        candidates = []
        for subseq_key, info in pattern_map.items():
            if info["count"] >= min_freq and len(info["sessions"]) >= 3:
                pattern = subseq_key.split("|")
                candidates.append({
                    "pattern": pattern,
                    "pattern_hash": self._hash_pattern(pattern),
                    "frequency": info["count"],
                    "sessions": list(info["sessions"]),
                    "task_summaries": info["task_summaries"],
                    "avg_total_steps": info["total_steps"] / info["count"],
                    "instances": info["instances"],
                })

        return candidates

    def _extract_instance_calls(
        self,
        tool_calls: List[Dict],
        subseq: tuple,
        approx_start: int,
    ) -> List[Dict]:
        """从 tool_calls 中提取与子序列对应的调用实例。"""
        # 过滤感知工具后的索引映射
        filtered = [(i, tc) for i, tc in enumerate(tool_calls) if tc["name"] not in PERCEPTION_TOOLS]

        # 从 approx_start 位置开始匹配
        if approx_start < len(filtered):
            result = []
            for offset, (_, tc) in enumerate(filtered[approx_start:approx_start + len(subseq)]):
                result.append(tc)
            if len(result) == len(subseq):
                return result

        # 回退：返回空列表
        return []

    # ═══════════════════════════════════════════════════
    # 层 2: 通用模式熵过滤
    # ═══════════════════════════════════════════════════

    def _layer2_universality(
        self,
        candidates: List[Dict],
        total_trajectories: int,
        threshold: float,
    ) -> List[Dict]:
        """过滤出现在过多轨迹中的「万金油」序列。"""
        if total_trajectories == 0:
            return candidates
        return [
            p for p in candidates
            if p["frequency"] / total_trajectories < threshold
        ]

    # ═══════════════════════════════════════════════════
    # 层 3: 参数结构聚类
    # ═══════════════════════════════════════════════════

    def _layer3_args_clustering(self, candidates: List[Dict]) -> List[Dict]:
        """对同一 tool name 序列的多次出现，按 args_pattern 做二次聚类。

        将 args_pattern 差异大的拆分为不同候选模式。
        """
        result = []
        for candidate in candidates:
            instances = candidate.get("instances", [])
            if len(instances) <= 1:
                result.append(candidate)
                continue

            # 提取每个 instance 的 args_pattern 指纹
            fingerprints = []
            for instance in instances:
                fp = self._instance_fingerprint(instance)
                fingerprints.append(fp)

            # 简单聚类：按指纹相似度分组
            clusters = self._simple_cluster(fingerprints, threshold=0.6)

            if len(clusters) == 1:
                # 所有实例属于同一工作流
                result.append(candidate)
            else:
                # 拆分为多个子模式
                for cluster_indices in clusters:
                    if len(cluster_indices) < 2:
                        continue  # 单个实例的子群不值得合成
                    sub_candidate = {
                        "pattern": candidate["pattern"],
                        "pattern_hash": self._hash_pattern(
                            candidate["pattern"],
                            extra=str(sorted(cluster_indices)),
                        ),
                        "frequency": len(cluster_indices),
                        "sessions": list({
                            candidate["sessions"][i]
                            for i in cluster_indices
                            if i < len(candidate["sessions"])
                        }),
                        "task_summaries": [
                            candidate["task_summaries"][i]
                            for i in cluster_indices
                            if i < len(candidate["task_summaries"])
                        ],
                        "avg_total_steps": candidate["avg_total_steps"],
                        "instances": [instances[i] for i in cluster_indices if i < len(instances)],
                    }
                    result.append(sub_candidate)

        return result

    @staticmethod
    def _instance_fingerprint(instance: List[Dict]) -> str:
        """生成一个实例的参数指纹字符串。"""
        parts = []
        for tc in instance:
            pattern = tc.get("args_pattern", {})
            if pattern:
                parts.append(json.dumps(pattern, sort_keys=True))
            else:
                parts.append(tc.get("name", ""))
        return "|".join(parts)

    @staticmethod
    def _simple_cluster(fingerprints: List[str], threshold: float = 0.6) -> List[List[int]]:
        """简单聚类：基于字符串相似度（Jaccard）。

        返回 [[index1, index2, ...], [index3, ...], ...]
        """
        n = len(fingerprints)
        if n == 0:
            return []

        # 将指纹转为 token 集合
        token_sets = []
        for fp in fingerprints:
            tokens = set(fp.split("|"))
            token_sets.append(tokens)

        # 贪心聚类
        assigned = [False] * n
        clusters = []

        for i in range(n):
            if assigned[i]:
                continue
            cluster = [i]
            assigned[i] = True
            for j in range(i + 1, n):
                if assigned[j]:
                    continue
                # Jaccard 相似度
                intersection = len(token_sets[i] & token_sets[j])
                union = len(token_sets[i] | token_sets[j])
                similarity = intersection / union if union > 0 else 0
                if similarity >= threshold:
                    cluster.append(j)
                    assigned[j] = True
            clusters.append(cluster)

        return clusters

    # ═══════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════

    def _load_trajectories(
        self,
        since_traj_id: Optional[str],
        window_size: int,
    ) -> List[Dict]:
        """加载轨迹（增量 + 窗口）。"""
        from core.trajectory_logger import TrajectoryLogger
        tl = TrajectoryLogger(self.project_dir)
        return tl.get_trajectories(since_traj_id=since_traj_id, limit=window_size)

    @staticmethod
    def _hash_pattern(pattern: List[str], extra: str = "") -> str:
        """生成 pattern 的稳定 hash。"""
        content = "|".join(pattern) + extra
        return hashlib.sha256(content.encode()).hexdigest()[:16]
