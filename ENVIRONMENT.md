# 运行环境基线

- Python：3.12.1
- 图像：Pillow 已安装
- PDF 生成：ReportLab 已安装
- PDF 检查与逐页渲染：PyMuPDF 已安装
- DOCX：python-docx 已安装
- XLSX：openpyxl 已安装
- PDF 读取：pypdf、pdfplumber 已安装
- 中文字体：微软雅黑、黑体、宋体、等线可用
- ImageMagick、FFmpeg 可用，但首版不依赖
- Chrome、LibreOffice、Ghostscript、Poppler CLI 不作为运行前提

首版仅使用 Python 已有依赖，避免浏览器打印和 Office 自动化。
