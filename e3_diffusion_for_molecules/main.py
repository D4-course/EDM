from fastapi import FastAPI

app = FastAPI()

from eval_analyze import main

@app.get("/sample/")
async def request_prediction(num_samples: int = 10):
    smiles_list = main(num_samples)
    print(smiles_list)
    return {"samples": smiles_list}
