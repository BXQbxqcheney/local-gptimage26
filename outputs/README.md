# outputs/

运行产物目录（已被 .gitignore 忽略，不入库）。

- `tasks/<task_id>/frame_XXX.png`：GIF 任务的每帧存档
- `tasks/<task_id>/animation.gif`：合成的 GIF 产物

说明：任务记录保存在内存中，服务重启后本目录文件保留，但任务 API（进度/下载端点）不可再访问；请及时下载需要的产物。
