from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# routers
from routers import ingredientbatches
from routers import materialbatches
from routers import merchandisebatches

app = FastAPI(title="Restock Microservice")

# include routers
app.include_router(ingredientbatches.router, prefix = '/ingredient-batches', tags=['ingredient batches'])
app.include_router(materialbatches.router, prefix = '/material-batches', tags=['material batches'])
app.include_router(merchandisebatches.router, prefix = '/merchandise-batches', tags=['merchandise batches'])

# CORS setup to allow frontend and backend on ports 
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # IMS
        "http://localhost:3000",  # ims frontend
        "http://192.168.100.10:3000",  # ims frontend (local network)
        "http://127.0.0.1:4000",  # auth service
        "http://127.0.0.1:8002",  # stock service
        "http://localhost:4000",  

        # POS
        "http://localhost:9000",  # frontend
        "http://192.168.100.10:9000",  

        # OOS
        "http://localhost:5000",  # frontend
        "http://192.168.100.10:5000",  
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Run app
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", port=8003, host="127.0.0.1", reload=True)