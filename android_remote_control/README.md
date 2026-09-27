# capLG Remote Android 客户端

这是 capLG 上位机的独立安卓遥控客户端。手机使用摄像头扫描上位机显示的二维码，自动连接上位机热点局域网HTTP服务。

## 当前功能

- CameraX + ML Kit 扫描二维码
- 解析 `http://电脑IP:端口/?code=配对码`
- 自动配对并保存 session token
- 显示上位机完整模组响应文本
- 人脸/手掌注册和识别
- 获取版本号、获取用户ID
- JPEG/RAW下载按钮

## 构建

使用 Android Studio 打开 `android_remote_control` 目录，等待 Gradle 同步后执行：

```bash
./gradlew assembleDebug
```

Windows PowerShell：

```powershell
.\gradlew.bat assembleDebug
```

APK输出：

```text
app/build/outputs/apk/debug/app-debug.apk
```

当前开发环境如果没有 Android SDK/Gradle，请直接用安装了 Android Studio 的电脑打开本目录构建。

## 使用

1. Windows 上位机开启“更多功能 → 手机远程控制”。
2. 在上位机连接信息窗口显示二维码。
3. 手机安装并打开本APK。
4. 允许摄像头权限。
5. 扫描二维码，客户端会自动完成配对。
6. 手机和电脑必须连接同一个手机热点或局域网。
7. Windows 防火墙需要允许 capLG 的局域网访问。

APK只调用受控HTTP动作，不发送任意串口帧、不执行OTA、不修改协议组。
