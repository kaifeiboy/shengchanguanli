#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目进度跟踪和测试系统

功能：
1. 实时进度监控
2. 自动化测试执行
3. 批量真实图片测试
4. 结果分析和报告生成
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 添加模块路径
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class ProjectProgressTracker:
    """项目进度跟踪器"""

    def __init__(self, project_root):
        self.project_root = project_root
        self.progress_db = os.path.join(project_root, "project_progress.db")
        self._init_database()

    def _init_database(self):
        """初始化进度数据库"""
        conn = sqlite3.connect(self.progress_db)
        cursor = conn.cursor()

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS phases (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            status TEXT DEFAULT 'pending',
            progress INTEGER DEFAULT 0
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            phase_id INTEGER,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            start_date TEXT,
            end_date TEXT,
            notes TEXT,
            FOREIGN KEY (phase_id) REFERENCES phases(id)
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,
            commits INTEGER DEFAULT 0,
            tests_passed INTEGER DEFAULT 0,
            tests_total INTEGER DEFAULT 0,
            tasks_completed INTEGER DEFAULT 0,
            issues INTEGER DEFAULT 0,
            notes TEXT
        )
        ''')

        conn.commit()
        conn.close()

    def initialize_phases(self):
        """初始化项目阶段"""
        phases = [
            (1, "Phase 1: 基础架构实施", "pending", 0),
            (2, "Phase 2: 数据收集和分析", "pending", 0),
            (3, "Phase 3: 智能调优和优化", "pending", 0)
        ]

        conn = sqlite3.connect(self.progress_db)
        cursor = conn.cursor()

        for phase_data in phases:
            cursor.execute('''
            INSERT OR IGNORE INTO phases (id, name, status, progress)
            VALUES (?, ?, ?, ?)
            ''', phase_data)

        conn.commit()
        conn.close()

    def update_phase_progress(self, phase_id, progress, status=None):
        """更新阶段进度"""
        conn = sqlite3.connect(self.progress_db)
        cursor = conn.cursor()

        if status:
            cursor.execute('''
            UPDATE phases SET progress=?, status=?, end_date=?
            WHERE id=?
            ''', (progress, status, datetime.now().isoformat(), phase_id))
        else:
            cursor.execute('''
            UPDATE phases SET progress=? WHERE id=?
            ''', (progress, phase_id))

        conn.commit()
        conn.close()

    def add_task(self, phase_id, task_name):
        """添加任务"""
        conn = sqlite3.connect(self.progress_db)
        cursor = conn.cursor()

        cursor.execute('''
        INSERT INTO tasks (phase_id, name, start_date)
        VALUES (?, ?, ?)
        ''', (phase_id, task_name, datetime.now().isoformat()))

        conn.commit()
        task_id = cursor.lastrowid
        conn.close()

        return task_id

    def complete_task(self, task_id, notes=None):
        """完成任务"""
        conn = sqlite3.connect(self.progress_db)
        cursor = conn.cursor()

        if notes:
            cursor.execute('''
            UPDATE tasks SET status='completed', end_date=?, notes=?
            WHERE id=?
            ''', (datetime.now().isoformat(), notes, task_id))
        else:
            cursor.execute('''
            UPDATE tasks SET status='completed', end_date=?
            WHERE id=?
            ''', (datetime.now().isoformat(), task_id))

        conn.commit()
        conn.close()

    def record_daily_report(self, commits, tests_passed, tests_total, tasks_completed, issues, notes=""):
        """记录日报"""
        conn = sqlite3.connect(self.progress_db)
        cursor = conn.cursor()

        today = datetime.now().strftime("%Y-%m-%d")

        cursor.execute('''
        INSERT OR REPLACE INTO daily_reports
        (date, commits, tests_passed, tests_total, tasks_completed, issues, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (today, commits, tests_passed, tests_total, tasks_completed, issues, notes))

        conn.commit()
        conn.close()

    def get_progress_summary(self):
        """获取进度摘要"""
        conn = sqlite3.connect(self.progress_db)
        cursor = conn.cursor()

        # 获取各阶段进度
        cursor.execute('SELECT name, progress, status FROM phases ORDER BY id')
        phases = cursor.fetchall()

        # 获取总体进度
        cursor.execute('SELECT AVG(progress) FROM phases')
        overall_progress = cursor.fetchone()[0] or 0

        # 获取今日统计
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute('''
        SELECT commits, tests_passed, tests_total, tasks_completed, issues
        FROM daily_reports WHERE date=?
        ''', (today,))
        today_stats = cursor.fetchone()

        conn.close()

        return {
            "phases": [{"name": p[0], "progress": p[1], "status": p[2]} for p in phases],
            "overall_progress": overall_progress,
            "today_stats": {
                "commits": today_stats[0] if today_stats else 0,
                "tests_passed": today_stats[1] if today_stats else 0,
                "tests_total": today_stats[2] if today_stats else 0,
                "tasks_completed": today_stats[3] if today_stats else 0,
                "issues": today_stats[4] if today_stats else 0
            } if today_stats else None
        }

    def generate_progress_report(self):
        """生成进度报告"""
        summary = self.get_progress_summary()

        report = f"""
# OCR优化项目进度报告

生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}

## 总体进度
进度: {summary['overall_progress']:.1f}%

## 各阶段详情
"""
        for phase in summary['phases']:
            status_icon = "✅" if phase['status'] == 'completed' else "🔄" if phase['status'] == 'in_progress' else "⏳"
            progress_bar = "█" * int(phase['progress'] / 5) + "░" * (20 - int(phase['progress'] / 5))
            report += f"""
### {phase['name']}
{status_icon} 进度: {progress_bar} {phase['progress']}%
状态: {phase['status']}
"""

        if summary['today_stats']:
            report += f"""
## 今日统计
- 代码提交: {summary['today_stats']['commits']}次
- 测试通过: {summary['today_stats']['tests_passed']}/{summary['today_stats']['tests_total']}
- 完成任务: {summary['today_stats']['tasks_completed']}个
- 待解决问题: {summary['today_stats']['issues']}个
"""

        return report


class RealImageTestRunner:
    """真实图片测试运行器"""

    def __init__(self, project_root):
        self.project_root = project_root
        self.test_data_dir = os.path.join(project_root, "test_data", "real_images")
        self.results_db = os.path.join(project_root, "test_results.db")
        self._init_test_database()

    def _init_test_database(self):
        """初始化测试数据库"""
        conn = sqlite3.connect(self.results_db)
        cursor = conn.cursor()

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_images (
            id INTEGER PRIMARY KEY,
            image_path TEXT NOT NULL,
            scenario TEXT NOT NULL,
            ground_truth TEXT,
            added_at TEXT
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY,
            image_id INTEGER,
            config_name TEXT NOT NULL,
            processing_time REAL,
            ocr_result TEXT,
            accuracy REAL,
            confidence REAL,
            status TEXT,
            error TEXT,
            tested_at TEXT,
            FOREIGN KEY (image_id) REFERENCES test_images(id)
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_configs (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            config_json TEXT NOT NULL,
            description TEXT
        )
        ''')

        conn.commit()
        conn.close()

    def add_test_images(self, image_paths, scenario, ground_truths=None):
        """批量添加测试图片"""
        conn = sqlite3.connect(self.results_db)
        cursor = conn.cursor()

        added_count = 0
        for i, image_path in enumerate(image_paths):
            if not os.path.exists(image_path):
                continue

            ground_truth = ground_truths[i] if ground_truths and i < len(ground_truths) else None

            cursor.execute('''
            INSERT INTO test_images (image_path, scenario, ground_truth, added_at)
            VALUES (?, ?, ?, ?)
            ''', (image_path, scenario, ground_truth, datetime.now().isoformat()))

            added_count += 1

        conn.commit()
        conn.close()

        return added_count

    def add_test_config(self, name, config, description=""):
        """添加测试配置"""
        conn = sqlite3.connect(self.results_db)
        cursor = conn.cursor()

        cursor.execute('''
        INSERT INTO test_configs (name, config_json, description)
        VALUES (?, ?, ?)
        ''', (name, json.dumps(config), description))

        conn.commit()
        conn.close()

    def run_single_test(self, image_path, config):
        """运行单个图片测试"""
        try:
            start_time = time.time()

            # 调用OCR处理
            result = self._run_ocr(image_path, config)

            processing_time = time.time() - start_time

            # 评估结果
            evaluation = self._evaluate_result(result, config)

            return {
                "status": "success",
                "processing_time": processing_time,
                "ocr_result": result.get("text", ""),
                "accuracy": evaluation.get("accuracy", 0.0),
                "confidence": result.get("confidence", 0.0),
                "evaluation": evaluation
            }

        except Exception as e:
            return {
                "status": "failed",
                "error": str(e),
                "processing_time": 0,
                "ocr_result": "",
                "accuracy": 0.0,
                "confidence": 0.0
            }

    def _run_ocr(self, image_path, config):
        """运行OCR处理（调用实际的OCR模块）"""
        # 这里应该调用实际的OCR处理逻辑
        # 示例实现：
        try:
            # 导入OCR模块
            from ocr_cli import main as ocr_main

            # 临时修改sys.argv来调用OCR
            original_argv = sys.argv
            sys.argv = ['ocr_cli.py', image_path]

            # 捕获输出
            from io import StringIO
            old_stdout = sys.stdout
            sys.stdout = StringIO()

            try:
                # 根据配置设置环境变量
                if config.get("use_preprocessing"):
                    os.environ["USE_PREPROCESSING"] = "1"
                if config.get("use_strict_decision"):
                    os.environ["USE_STRICT_DECISION"] = "1"

                # 执行OCR
                ocr_main()

                # 获取结果
                output = sys.stdout.getvalue()
                sys.stdout = old_stdout

                return {
                    "text": output.strip(),
                    "confidence": 0.85  # 示例值
                }

            finally:
                sys.argv = original_argv
                sys.stdout = old_stdout

        except Exception as e:
            return {"text": "", "error": str(e)}

    def _evaluate_result(self, result, config):
        """评估OCR结果"""
        # 这里应该实现实际的结果评估逻辑
        # 示例实现：
        text = result.get("text", "")

        evaluation = {
            "accuracy": 0.0,
            "completeness": 0.0,
            "key_info_found": False
        }

        # 检查关键信息
        if "PC-" in text or "QHR" in text:
            evaluation["key_info_found"] = True
            evaluation["accuracy"] += 0.3

        if "400" in text and len(text) > 10:
            evaluation["accuracy"] += 0.3

        # 基于文本长度评估完整性
        if len(text) > 5:
            evaluation["completeness"] = min(1.0, len(text) / 20)
            evaluation["accuracy"] += 0.4 * evaluation["completeness"]

        return evaluation

    def run_batch_tests(self, config_name, scenario=None, num_workers=4):
        """批量运行测试"""
        conn = sqlite3.connect(self.results_db)
        cursor = conn.cursor()

        # 获取测试配置
        cursor.execute('SELECT config_json FROM test_configs WHERE name=?', (config_name,))
        config_row = cursor.fetchone()
        if not config_row:
            conn.close()
            raise ValueError(f"测试配置 '{config_name}' 不存在")

        config = json.loads(config_row[0])

        # 获取测试图片
        if scenario:
            cursor.execute('''
            SELECT id, image_path, ground_truth FROM test_images
            WHERE scenario=?
            ''', (scenario,))
        else:
            cursor.execute('SELECT id, image_path, ground_truth FROM test_images')

        test_images = cursor.fetchall()
        conn.close()

        # 分割任务
        chunks = [test_images[i::num_workers] for i in range(num_workers)]

        # 并行执行
        results = []
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = []
            for chunk in chunks:
                for image_id, image_path, ground_truth in chunk:
                    future = executor.submit(
                        self._run_and_save_test,
                        image_id, image_path, config_name, config, ground_truth
                    )
                    futures.append(future)

            for future in as_completed(futures):
                results.append(future.result())

        return results

    def _run_and_save_test(self, image_id, image_path, config_name, config, ground_truth):
        """运行测试并保存结果"""
        test_result = self.run_single_test(image_path, config)

        # 保存到数据库
        conn = sqlite3.connect(self.results_db)
        cursor = conn.cursor()

        cursor.execute('''
        INSERT INTO test_results
        (image_id, config_name, processing_time, ocr_result, accuracy, confidence, status, error, tested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            image_id,
            config_name,
            test_result["processing_time"],
            test_result["ocr_result"],
            test_result["accuracy"],
            test_result["confidence"],
            test_result["status"],
            test_result.get("error", ""),
            datetime.now().isoformat()
        ))

        conn.commit()
        conn.close()

        return {
            "image_id": image_id,
            "config_name": config_name,
            **test_result
        }

    def analyze_results(self, config_name):
        """分析测试结果"""
        conn = sqlite3.connect(self.results_db)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
            AVG(processing_time) as avg_time,
            AVG(accuracy) as avg_accuracy,
            AVG(confidence) as avg_confidence
        FROM test_results
        WHERE config_name=?
        ''', (config_name,))

        row = cursor.fetchone()
        if not row or row[0] == 0:
            conn.close()
            return None

        total, success, avg_time, avg_accuracy, avg_confidence = row

        # 按场景分析
        cursor.execute('''
        SELECT
            ti.scenario,
            COUNT(*) as count,
            AVG(tr.processing_time) as avg_time,
            AVG(tr.accuracy) as avg_accuracy
        FROM test_results tr
        JOIN test_images ti ON tr.image_id = ti.id
        WHERE tr.config_name=?
        GROUP BY ti.scenario
        ''', (config_name,))

        scenario_stats = {}
        for scenario, count, avg_time, avg_accuracy in cursor.fetchall():
            scenario_stats[scenario] = {
                "count": count,
                "avg_processing_time": avg_time,
                "avg_accuracy": avg_accuracy
            }

        conn.close()

        return {
            "config_name": config_name,
            "total_tests": total,
            "success_count": success,
            "success_rate": success / total if total > 0 else 0,
            "avg_processing_time": avg_time,
            "avg_accuracy": avg_accuracy,
            "avg_confidence": avg_confidence,
            "scenario_stats": scenario_stats
        }

    def generate_comparison_report(self, config_names):
        """生成配置对比报告"""
        reports = []

        for config_name in config_names:
            analysis = self.analyze_results(config_name)
            if analysis:
                reports.append(analysis)

        if not reports:
            return "无测试数据可供分析"

        comparison = f"""
# OCR配置对比报告

生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}

## 性能对比
"""

        # 性能对比表格
        comparison += "| 配置 | 测试数 | 成功率 | 平均时间 | 平均准确率 |\n"
        comparison += "|------|--------|--------|----------|------------|\n"

        for report in reports:
            comparison += (
                f"| {report['config_name']} | "
                f"{report['total_tests']} | "
                f"{report['success_rate']*100:.1f}% | "
                f"{report['avg_processing_time']*1000:.0f}ms | "
                f"{report['avg_accuracy']*100:.1f}% |\n"
            )

        # 按场景详细对比
        comparison += "\n## 按场景详细分析\n\n"

        scenarios = set()
        for report in reports:
            scenarios.update(report['scenario_stats'].keys())

        for scenario in sorted(scenarios):
            comparison += f"### {scenario}\n\n"
            comparison += "| 配置 | 测试数 | 平均时间 | 平均准确率 |\n"
            comparison += "|------|--------|----------|------------|\n"

            for report in reports:
                stats = report['scenario_stats'].get(scenario, {})
                comparison += (
                    f"| {report['config_name']} | "
                    f"{stats.get('count', 0)} | "
                    f"{stats.get('avg_processing_time', 0)*1000:.0f}ms | "
                    f"{stats.get('avg_accuracy', 0)*100:.1f}% |\n"
                )

            comparison += "\n"

        return comparison


def main():
    """主函数 - 演示使用"""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 1. 初始化项目进度跟踪
    tracker = ProjectProgressTracker(project_root)
    tracker.initialize_phases()

    # 2. 添加当前任务
    task_id = tracker.add_task(1, "Phase 1.1: 创建图像预处理模块")

    # 3. 完成任务
    tracker.complete_task(task_id, "完成预处理模块，单元测试覆盖率95%")

    # 4. 记录日报
    tracker.record_daily_report(
        commits=3,
        tests_passed=15,
        tests_total=15,
        tasks_completed=1,
        issues=0,
        notes="预处理模块开发完成，开始Phase 1.2"
    )

    # 5. 更新阶段进度
    tracker.update_phase_progress(1, 20, "in_progress")

    # 6. 生成进度报告
    progress_report = tracker.generate_progress_report()
    print(progress_report)

    # 7. 初始化测试系统
    test_runner = RealImageTestRunner(project_root)

    # 8. 添加测试配置
    baseline_config = {
        "use_preprocessing": False,
        "use_strict_decision": False,
        "second_ocr_scaling": True
    }
    test_runner.add_test_config("baseline", baseline_config, "原始配置（基线）")

    optimized_config = {
        "use_preprocessing": True,
        "use_strict_decision": True,
        "second_ocr_scaling": False
    }
    test_runner.add_test_config("optimized", optimized_config, "优化配置（预处理+严格分工）")

    # 9. 添加测试图片（示例）
    test_images = [
        r"e:\workaaa\shengchanguanli\vq_closeup.jpg",
        r"e:\workaaa\shengchanguanli\jq_closeup.jpg"
    ]
    test_runner.add_test_images(test_images, "normal_light")

    # 10. 运行测试
    print("\n开始运行测试...")
    baseline_results = test_runner.run_batch_tests("baseline", num_workers=2)
    print(f"基线配置测试完成: {len(baseline_results)}个测试")

    # 11. 分析结果
    baseline_analysis = test_runner.analyze_results("baseline")
    if baseline_analysis:
        print(f"\n基线配置分析:")
        print(f"成功率: {baseline_analysis['success_rate']*100:.1f}%")
        print(f"平均处理时间: {baseline_analysis['avg_processing_time']*1000:.0f}ms")
        print(f"平均准确率: {baseline_analysis['avg_accuracy']*100:.1f}%")

    # 12. 生成对比报告
    comparison_report = test_runner.generate_comparison_report(["baseline", "optimized"])
    print(comparison_report)


if __name__ == "__main__":
    main()