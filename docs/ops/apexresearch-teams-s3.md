# ApexResearch 团队 S3 存储使用说明

## 一、这是什么

团队共享的 S3 存储桶，用于存放各成员的项目文件（运行日志、轨迹 trajectories 等）。

- **桶名**：`apexresearch-teams`
- **区域**：`ap-northeast-1`（东京）
- **权限**：每人只能读写自己名下的目录 `apexresearch-teams/<你的用户名>/*`，看不到也动不了别人的
- **特性**：私有 · 服务端加密 · 开启版本控制（误删/覆盖可恢复历史版本）

> 「用户名」= 你的 AWS IAM 用户名（如 `wujunhao`、`huangyixiao`）。

## 二、准备（一次性）

安装 AWS CLI 并配置你的访问密钥（Access Key）：

```bash
# 安装 AWS CLI（若未安装）
#   macOS:  brew install awscli
#   Linux:  sudo apt install awscli   或   pip install awscli

# 配置密钥
aws configure set aws_access_key_id     <你的 AccessKeyId>
aws configure set aws_secret_access_key <你的 SecretAccessKey>
aws configure set region ap-northeast-1

# 验证（能返回你的用户名即成功）
aws sts get-caller-identity
```

> 没有 Access Key？找管理员创建一把。

## 三、常用命令

把命令里的 `<me>` 换成你自己的用户名，只能在 `<me>/` 下操作。

```bash
# 上传单个文件
aws s3 cp ./run.log s3://apexresearch-teams/<me>/logs/run.log

# 上传整个目录（增量同步，推荐传日志/轨迹）
aws s3 sync ./trajectories s3://apexresearch-teams/<me>/trajectories/

# 查看自己的文件
aws s3 ls s3://apexresearch-teams/<me>/ --recursive

# 下载
aws s3 cp   s3://apexresearch-teams/<me>/logs/run.log ./run.log
aws s3 sync s3://apexresearch-teams/<me>/trajectories/ ./traj_local/

# 删除
aws s3 rm s3://apexresearch-teams/<me>/logs/run.log
aws s3 rm s3://apexresearch-teams/<me>/oldstuff/ --recursive
```

推荐的目录约定（可自选）：

```
<me>/
├── logs/          # 运行日志
├── trajectories/  # 轨迹
└── <项目名>/       # 按项目再分
```

## 四、注意事项

- ❌ 不能列出桶根，也不能访问别人的目录（`aws s3 ls s3://apexresearch-teams/` 会 AccessDenied）——设计如此。
- ✅ 目录无需手动创建，第一次 `cp`/`sync` 到 `<me>/xxx/` 时自动生成。
- 🔁 传大量文件优先用 `sync`（只传变化部分，更快更省）。
- 🗑️ 已开版本控制：删除/覆盖的旧版本仍会保留（可恢复），因此「删掉」不等于立刻释放空间。
- 🔒 东京区的 EC2 访问该桶走 AWS 内网，无需公网、速度快。

## 五、常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `Unable to locate credentials` | 没配置密钥，回到第二步 |
| `AccessDenied`（访问自己目录时） | 检查路径里的用户名是否和 `aws sts get-caller-identity` 返回的一致 |
| `AccessDenied`（访问桶根/别人目录） | 正常，权限只限自己前缀 |
| 传了大文件很慢 | 用 `aws s3 sync`；或在东京区机器上操作走内网 |

---

## 管理员备注

- 权限由托管策略 `S3TeamOwnPrefix`（`arn:aws:iam::297645381734:policy/S3TeamOwnPrefix`）统一授予，使用 IAM 变量 `${aws:username}` 自动限定到各自前缀，已挂给团队成员 IAM 用户。
- 新成员加入：给其 IAM 用户 attach `S3TeamOwnPrefix` 即可。
- 桶已开启 Block Public Access（四项全开）+ 版本控制 + 默认加密（SSE-S3）。
