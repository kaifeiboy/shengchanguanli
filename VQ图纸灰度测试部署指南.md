# VQ图纸产品级灰度测试部署指南

## 一、部署准备

### 1.1 环境检查
- [ ] 确认测试环境与生产环境隔离
- [ ] 备份现有生产数据库
- [ ] 确认VQ图纸产品在生产环境中的正常使用情况
- [ ] 准备监控面板和告警配置

### 1.2 文件清单
确认以下文件已准备就绪：
- `src/Platform/Modules/Drawings/ocr_cli.py` (已更新)
- `src/Platform/Modules/Drawings/grayscale_config.json` (新建)
- `src/Platform/Modules/Drawings/smart_image_preprocessor.py`
- `src/Platform/Modules/Drawings/smart_ocr_decision.py`
- `src/Platform/Modules/Drawings/ocr_model_manager.py`

## 二、配置说明

### 2.1 灰度配置文件
位置：`src/Platform/Modules/Drawings/grayscale_config.json`

```json
{
  "grayscale": {
    "enabled": true,
    "products": ["VQ图纸"],
    "description": "产品级灰度测试配置，仅指定产品使用优化版本"
  },
  "monitoring": {
    "log_performance": true,
    "log_errors": true,
    "performance_threshold_seconds": 10.0,
    "success_rate_threshold": 0.95
  }
}
```

### 2.2 关键配置说明
- `enabled`: 是否启用灰度测试（true/false）
- `products`: 使用优化版本的产品列表
- `log_performance`: 是否记录性能日志
- `performance_threshold_seconds`: 性能告警阈值（秒）

## 三、部署步骤

### 3.1 代码部署
```bash
# 1. 拉取最新代码
git pull origin main

# 2. 验证文件完整性
ls -la src/Platform/Modules/Drawings/grayscale_config.json
ls -la src/Platform/Modules/Drawings/smart_*.py

# 3. 检查配置文件
cat src/Platform/Modules/Drawings/grayscale_config.json
```

### 3.2 配置验证
```bash
# 测试配置文件加载
python -c "
import sys
sys.path.insert(0, 'src/Platform/Modules/Drawings')
import ocr_cli
print('灰度配置加载成功')
print('VQ图纸是否使用优化版本:', ocr_cli.should_use_optimized_ocr('VQ图纸'))
print('其他产品是否使用优化版本:', ocr_cli.should_use_optimized_ocr('其他产品'))
"
```

### 3.3 功能测试
```bash
# 测试VQ图纸产品（应使用优化版本）
export OCR_PRODUCT_NAME="VQ图纸"
python src/Platform/Modules/Drawings/ocr_cli.py 测试图片/aa581999acdef817401e39e08739f65d_compress.jpg

# 测试其他产品（应使用原始版本）
export OCR_PRODUCT_NAME="其他产品"
python src/Platform/Modules/Drawings/ocr_cli.py 测试图片/aa581999acdef817401e39e08739f65d_compress.jpg
```

## 四、监控配置

### 4.1 性能监控
监控指标：
- 处理时间（目标：<10s）
- 成功率（目标：>95%）
- 错误率（目标：<0.1%）

### 4.2 日志监控
关键日志位置：
- 性能日志：`data/ocr/performance.log`
- 错误日志：`data/ocr/ocr_crash.log`

### 4.3 告警配置
建议告警规则：
- 处理时间 > 10秒：立即告警
- 成功率 < 95%：立即告警
- 连续3次失败：立即告警

## 五、回滚方案

### 5.1 快速回滚（配置级）
```bash
# 方法1：修改配置文件
vim src/Platform/Modules/Drawings/grayscale_config.json
# 将 "enabled": true 改为 "enabled": false

# 方法2：清空产品列表
# 将 "products": ["VQ图纸"] 改为 "products": []
```

### 5.2 完整回滚（代码级）
```bash
# 回滚到上一个稳定版本
git revert <commit_hash>
git push origin main
```

## 六、验证检查清单

### 6.1 部署前检查
- [ ] 配置文件正确设置
- [ ] 仅VQ图纸在灰度产品列表中
- [ ] 其他产品不在列表中
- [ ] 监控和告警已配置
- [ ] 回滚方案已准备

### 6.2 部署后验证
- [ ] VQ图纸产品使用优化版本
- [ ] 其他产品使用原始版本
- [ ] 性能指标正常
- [ ] 错误日志无异常
- [ ] 用户反馈正常

### 6.3 持续监控（3-5天）
- [ ] 每日检查性能指标
- [ ] 每日检查错误日志
- [ ] 收集用户反馈
- [ ] 对比新旧版本数据

## 七、应急联系

### 7.1 技术支持
- 开发负责人：[待填写]
- 运维负责人：[待填写]
- 产品负责人：[待填写]

### 7.2 应急流程
1. 发现问题 → 立即执行配置级回滚
2. 通知相关责任人
3. 分析问题根因
4. 修复问题后重新测试
5. 重新部署验证

## 八、成功标准

### 8.1 灰度测试成功标准
- [ ] 连续3-5天无重大问题
- [ ] 处理时间 < 10秒
- [ ] 成功率 > 95%
- [ ] 用户反馈良好
- [ ] 无新的识别错误模式

### 8.2 进入全量发布条件
- [ ] 灰度测试成功标准全部达成
- [ ] 性能明显优于原始版本
- [ ] 识别准确率保持或提升
- [ ] 系统稳定性良好

---

**部署时间**：[待填写]
**部署人员**：[待填写]
**审核人员**：[待填写]
**文档版本**：v1.0
**状态**：待执行