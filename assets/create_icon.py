"""用 Qt 生成原创简单图标，无额外绘图库依赖。"""
from pathlib import Path
import struct
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF
from PySide6.QtGui import QImage, QPainter, QColor, QPolygonF, QPen


def main():
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor('#2563c7'), 0))
    painter.setBrush(QColor('#2563c7'))
    painter.drawRoundedRect(8, 8, 240, 240, 48, 48)
    painter.setBrush(QColor('#ffffff'))
    painter.setPen(QPen(QColor('#ffffff'), 0))
    painter.drawRoundedRect(110, 50, 36, 90, 8, 8)
    painter.drawPolygon(QPolygonF([QPointF(72, 121), QPointF(184, 121), QPointF(128, 178)]))
    painter.setPen(QPen(QColor('#ffffff'), 14))
    painter.drawLine(72, 204, 184, 204)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    payload = bytes(data)
    target = Path(__file__).resolve().parent / 'app.ico'
    target.write_bytes(struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', 0, 0, 0, 0, 1, 32, len(payload), 22) + payload)


if __name__ == '__main__':
    main()
