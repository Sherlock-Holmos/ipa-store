# Sherlock-Holmos LiveContainer IPA 源

这是一个可以直接被 LiveContainer 读取的 AltSource 模板，同时兼容 AltStore / SideStore 的基础源格式。

## 快速部署到 GitHub Pages

1. 把这个目录推送到 GitHub 仓库的 `main` 分支。
2. 编辑 [`source.config.json`](source.config.json)，至少修改 `name`、`identifier` 和 `publicBaseURL`。例如：

   ```json
   "publicBaseURL": "https://你的用户名.github.io/仓库名"
   ```

3. 在仓库 Settings → Pages → Build and deployment 中选择 **GitHub Actions**。
4. 等待 `Deploy source to GitHub Pages` 成功后，源地址就是：

   ```text
   https://你的用户名.github.io/仓库名/source.json
   ```

5. 在 LiveContainer → Sources 中添加这个 URL，然后刷新。

当前仓库没有 IPA 时，`source.json` 是一个合法的空源，不会显示虚假的下载条目。

## 添加 IPA

在这个仓库创建 GitHub Release，把一个或多个 `.ipa` 文件作为 Release asset 上传并发布。`Update AltSource` 工作流会自动：

- 读取 IPA 内的 `Payload/*.app/Info.plist`；
- 提取 Bundle ID、应用名、版本、最低系统版本和文件大小；
- 从 IPA 提取 PNG 图标到 `icons/`；
- 使用 GitHub Release 的下载地址和发布日期生成 `source.json`；
- 提交变更，随后 GitHub Pages 自动部署。

Release asset 的文件名可以任意，只要扩展名是 `.ipa`。多个 Release 中相同 Bundle ID 会合并为一个 App，并按日期倒序展示版本。

如果应用名称、开发者或简介不理想，可以在 [`app-overrides.json`](app-overrides.json) 中按 Bundle ID 覆盖：

```json
{
  "com.example.app": {
    "name": "我的应用",
    "developerName": "开发者名称",
    "subtitle": "一句话副标题",
    "localizedDescription": "应用介绍",
    "category": "utilities",
    "tintColor": "#635BFF"
  }
}
```

支持的常用覆盖字段还包括 `beta`、`versionDescription`、`iconURL`、`screenshotURLs` 和 `appPermissions`。

## 手动本地生成

如果不使用 GitHub Releases，也可以把 IPA 临时放进 `ipas/` 目录，然后运行：

```bash
python scripts/build_source.py --ipa-dir ipas --public-base-url https://example.com
python scripts/build_source.py --check
```

本地模式会把 IPA 文件名写入 `downloadURL`，适合自有服务器部署前测试；生产环境请把它们放在 HTTPS 可访问的位置。

## URL Scheme

LiveContainer 支持通过下面的形式添加源：

```text
livecontainer://sources?url=https%3A%2F%2F你的用户名.github.io%2F仓库名%2Fsource.json
```

## 注意事项

- IPA 必须是你有权分发的文件；源本身不负责签名，安装/签名仍由 LiveContainer、AltStore 或 SideStore 处理。
- 下载地址和图标地址必须能通过 HTTPS 访问，且服务器应返回正确的文件内容。
- GitHub Actions 使用仓库自带的 `GITHUB_TOKEN`，不需要额外配置密钥。
- 如果不想包含预发布版本，将 `source.config.json` 中的 `includePrereleases` 保持为 `false`；需要时改成 `true`。
