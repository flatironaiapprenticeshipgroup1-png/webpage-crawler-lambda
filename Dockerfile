FROM public.ecr.aws/lambda/python:3.12

RUN dnf install -y atk cups-libs gtk3 libXcomposite libXcursor libXdamage \
    libXext libXi libXrandr libXScrnSaver libXtst pango at-spi2-atk \
    libXt xorg-x11-server-Xvfb nss mesa-libgbm alsa-lib

COPY handler.py crawler.py html_regenerator.py image_downloader.py status_publisher.py requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install -r requirements.txt && \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright playwright install chromium && \
    chmod -R 777 /ms-playwright

CMD ["handler.lambda_handler"]
