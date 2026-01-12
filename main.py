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
        "https://bleu-ims-beta.vercel.app",  # ims frontend
        "https://authservices-npr8.onrender.com",  # auth service
        "https://bleu-stockservices.onrender.com",  # stock service
        
        # POS
        "https://sales-services.onrender.com", #sales service

        # OOS
        "https://bleu-oos-rouge.vercel.app",  # frontend
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Run app
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", port=8003, host="127.0.0.1", reload=True)