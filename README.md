# 角色唱歌

AstrBot 插件与轻量唱歌网关。B 站搜索和音轨下载在服务器完成，人声分离和 RVC 音色转换调用 RunningHub 消费级 API。

## 使用

- `/点歌 青衣`：搜索版本后 120 秒内回复 `1` 等序号选歌，回复 `0` 退出。
- `/点歌 BV1LiVKzFEVf`：直接生成，默认从第 60 秒开始，唱满 60 秒后寻找静音句尾，最长 240 秒，带伴奏。
- `/点歌 BV1LiVKzFEVf 从60秒 降调2`：自定义起点和音高。短歌自动前移起点，找不到静音句尾时截到最长时长或歌曲结尾。
- `/唱歌进度`：查询当前任务。
- `/纯人声`、`/带伴奏`：生成成功后 10 分钟内获取上一首的对应版本，到期后静默处理。
- `/点歌转发 123456789`：在原会话中，生成成功后 10 分钟内转发你自己的上一首，复用已生成的音频。支持 OneBot QQ，目标需是机器人已加入的群或已添加的好友。号码重复时用 `/点歌转发 群 123456789` 或 `/点歌转发 QQ 123456789` 指定类型。旧版未记录音频版本时发送带伴奏版。

搜索会显示编号、标题、UP 主、时长和视频链接，按 B 站返回顺序展示。自然对话使用 `sing_song` 搜歌，再用 `confirm_song` 提交用户选中的序号。插件配置可设置起点和升降调，默认保留原曲音高。

## 部署

服务器需要 Python 3.12+、ffmpeg、ffprobe。网关代码、虚拟环境、运行数据分别放置：

```
/www/ahuai/rvc-singing
/www/ahuai/rvc-singing-runtime
/www/ahuai/rvc-singing-data
```

在独立虚拟环境中安装 `gateway/requirements.txt`，配置权限为 600 的 `gateway.env`：

```
RVC_GATEWAY_TOKEN=<随机令牌，至少24位>
RUNNINGHUB_API_KEY=<消费级API密钥>
RVC_WORKFLOW_ID=<私有RVC工作流ID>
RVC_MODEL_FILE=<RunningHub模型文件名>
BILIBILI_COOKIES_FILE=<Netscape Cookie文件路径>
RVC_DATA_DIR=/www/ahuai/rvc-singing-data
```

使用 `gateway/rvc-singing.service` 启动网关，默认仅监听 `127.0.0.1:9002`。将 `astrbot_plugin_rvc_singing` 放入 AstrBot 插件目录，配置相同网关令牌。AstrBot 在容器中时，需要能访问宿主机地址；当前部署使用 host 网络。

网关串行执行任务，在 SQLite 中保存任务号。同一片段可复用缓存，默认保留 7 天，最多 50 条成品，超出后删除最旧的缓存。截取策略沿用原网关：起点不超过歌曲总长减 61 秒，以 -35 dB、持续 0.5 秒检测静音；满 60 秒后找到首个合适的静音起点即收尾，最多截取 240 秒。云端已受理的任务会继续查询；若提交请求期间中断且未取得任务号，需要先去 RunningHub 核对任务，避免重复计费。分离和转换分别按平台实际用量扣积分。B 站接口受访问限制时可能需要更新 Cookie。

## 开发验证

把工程复制到项目外临时目录，在隔离虚拟环境执行 `python -B -m pytest -q -p no:cacheprovider`。代码使用 Ruff 格式化和检查。密钥及部署私有文件不应提交到仓库。

MIT License · ahuai
