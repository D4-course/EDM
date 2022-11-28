FROM continuumio/anaconda3

WORKDIR /src/
COPY . .
WORKDIR /src/e3_diffusion_for_molecules
RUN apt-get update && apt-get install -y gcc libstdc++6
RUN cp /opt/conda/lib/libstdc++.so.6.0.29 /usr/lib/x86_64-linux-gnu/
RUN cp /usr/lib/x86_64-linux-gnu/libstdc++.so.6.0.29 /usr/lib/x86_64-linux-gnu/libstdc++.so.6
RUN pip install rdkit fastapi uvicorn
RUN pip install -r requirements.txt

CMD ["uvicorn", "main:app", "--proxy-headers", "--host", "0.0.0.0", "--port", "80"]


