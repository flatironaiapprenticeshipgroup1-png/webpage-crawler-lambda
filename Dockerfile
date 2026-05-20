FROM public.ecr.aws/lambda/python:3.12

RUN dnf install -y atk cups-libs gtk3 libXcomposite libXcursor libXdamage \
    libXext libXi libXrandr libXScrnSaver libXtst pango at-spi2-atk \
    libXt xorg-x11-server-Xvfb nss mesa-libgbm alsa-lib && \
    pip install playwright && playwright install chromium

COPY handler.py crawler.py requirements.txt ./
RUN pip install -r requirements.txt

CMD ["handler.lambda_handler"]
