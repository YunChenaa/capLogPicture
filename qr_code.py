"""可选的离线二维码生成工具。依赖未安装时返回友好错误，不影响主程序启动。"""


def generate_qr_image(text, box_size=8, border=4):
    """返回可交给QPixmap.fromImage的二维码QImage。"""
    try:
        import qrcode
    except ImportError as exc:
        raise RuntimeError(
            '未安装二维码依赖，请执行：python -m pip install qrcode[pil]'
        ) from exc
    from PySide6.QtGui import QImage

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    width, height = image.size
    raw = image.tobytes('raw', 'RGB')
    qimage = QImage(raw, width, height, width * 3, QImage.Format_RGB888)
    return qimage.copy()
