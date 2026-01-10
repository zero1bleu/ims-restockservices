from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Union
from datetime import date, datetime
from decimal import Decimal
import httpx
import decimal as _decimal
from database import get_db_connection
import logging

logger = logging.getLogger(__name__)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://localhost:4000/auth/token")
router = APIRouter()

BLOCKCHAIN_RESTOCK_URL = "http://localhost:8006/blockchain/restock"
BLOCKCHAIN_WASTE_URL = "http://localhost:8006/blockchain/waste"

# helper to get user id from token
async def get_user_id_from_token(token: str) -> int:
    USER_SERVICE_ME_URL = "http://localhost:4000/auth/users/me"
    async with httpx.AsyncClient() as client:
        response = await client.get(USER_SERVICE_ME_URL, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        user_data = response.json()
        return user_data.get("userId") or user_data.get("id")
    
# convert decimal to float
def convert_decimal_to_float(obj):
    if isinstance(obj, dict):
        return {k: convert_decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_decimal_to_float(v) for v in obj]
    # support Decimal from decimal module
    if isinstance(obj, _decimal.Decimal):
        return float(obj)
    return obj

# threshold for stock status
thresholds = {
    "g": 50, "kg": 0.5, "ml": 100, "l": 0.5,
}

# get batch status based on amount and expiration
def get_batch_status(ingredient_amount, expiration_date):
    today = date.today()
    if ingredient_amount <= 0:
        return "Used"
    elif expiration_date and expiration_date <= today:
        return "Expired"
    else:
        return "Available"

# get ingredient status based on amount, measurement and expiration
def get_ingredient_status(amount, measurement, expiration_date):
    today = date.today()
    if amount <= 0:
        return "Not Available"
    if expiration_date and expiration_date <= today:
        return "Expired"
    meas_lower = (measurement or "").lower()
    threshold = thresholds.get(meas_lower, 1)
    if amount <= threshold:
        return "Low Stock"
    return "Available"

# notif tigger
async def trigger_low_stock_notification(token):
    STOCKSERVICES_URL = "http://127.0.0.1:8002/notifications/check-low-stock-after-waste"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"Triggering low stock notification via HTTP: {STOCKSERVICES_URL}")
            response = await client.post(STOCKSERVICES_URL, headers=headers)
            logger.info(f"Low stock notification response: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to trigger low stock notification: {e}")

# log auto waste
async def log_auto_waste(conn, ingredient_id, amount, unit, reason, logged_by, notes=None):
    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            INSERT INTO WasteManagement (
                ItemType, ItemID, BatchID, Amount, Unit, WasteReason, WasteDate, LoggedBy, Notes
            )
            OUTPUT INSERTED.*
            VALUES (?, ?, NULL, ?, ?, ?, GETDATE(), ?, ?)
            """,
            ("ingredient", ingredient_id, amount, unit, reason, logged_by, notes)
        )
        inserted = await cursor.fetchone()
        return inserted
    
# auth validation
async def validate_token_and_roles(token: str, allowed_roles: List[str]):
    USER_SERVICE_ME_URL = "http://localhost:4000/auth/users/me"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(USER_SERVICE_ME_URL, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            error_detail = f"Auth service error: {e.response.status_code}"
            try: error_detail += f" - {e.response.json().get('detail', e.response.text)}"
            except: error_detail += f" - {e.response.text}"
            logger.error(error_detail)
            raise HTTPException(status_code=e.response.status_code, detail=error_detail)
        except httpx.RequestError as e:
            logger.error(f"Auth service unavailable: {e}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Auth service unavailable: {e}")

    user_data = response.json()
    user_role = user_data.get("userRole")
    if user_role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Required role not met. User has role: '{user_role}'"
        )

# models
class IngredientBatchCreate(BaseModel):
    ingredient_id: int
    quantity: float
    unit: str
    batch_date: date
    expiration_date: date
    logged_by: str
    notes: Optional[str] = None

class IngredientBatchUpdate(BaseModel):
    quantity: Optional[float]
    unit: Optional[str]
    batch_date: Optional[date]
    expiration_date: Optional[date]
    logged_by: Optional[str]
    notes: Optional[str]

class IngredientBatchOut(BaseModel):
    batch_id: int
    ingredient_id: int
    ingredient_name: str
    quantity: float
    unit: str
    batch_date: date
    expiration_date: date
    restock_date: datetime
    logged_by: str
    notes: Optional[str]
    status: str

# restock ingredient
@router.post("/", response_model=Union[IngredientBatchOut, Dict])
async def create_batch(batch: IngredientBatchCreate, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "manager", "staff"])
    conn = await get_db_connection()
    auto_waste_inserted = None
    tx_hash_for_restock: Optional[str] = None
    try:
        conn.autocommit = False  
        async with conn.cursor() as cursor:
            today = date.today()
            
            # determine batch status
            batch_status = "Available"
            if batch.expiration_date <= today:
                batch_status = "Expired"
            elif batch.quantity == 0:
                batch_status = "Used"

            # fetch current ingredient data
            await cursor.execute(
                "SELECT Amount, ExpirationDate, Measurement, IngredientName FROM Ingredients WHERE IngredientID = ?", 
                batch.ingredient_id
            )
            ingredient_row = await cursor.fetchone()
            if not ingredient_row:
                await conn.rollback()
                raise HTTPException(status_code=404, detail="Ingredient not found")
            
            current_amount = ingredient_row.Amount
            current_expiration = ingredient_row.ExpirationDate
            ingredient_unit = ingredient_row.Measurement
            ingredient_name = ingredient_row.IngredientName
            
            # calculate new amount
            was_expired = (current_expiration and current_expiration < today)
            
            # log auto waste if replacing expired stock
            if was_expired and current_amount > 0:
                auto_waste_inserted = await log_auto_waste(
                    conn,
                    batch.ingredient_id,
                    current_amount,
                    ingredient_unit,
                    reason="Expired stock",
                    logged_by=batch.logged_by,
                    notes="Auto waste log for expired stock after restock"
                )
                new_amount = Decimal(str(batch.quantity))
            else:
                new_amount = (Decimal(str(current_amount)) if isinstance(current_amount, (int, float)) else current_amount) + Decimal(str(batch.quantity))
            
            # calculate new status based on new amount
            new_status = get_ingredient_status(new_amount, ingredient_unit, batch.expiration_date)
            
            # insert new batch
            await cursor.execute("""
                INSERT INTO IngredientBatches 
                (IngredientID, Quantity, Unit, BatchDate, ExpirationDate, RestockDate, LoggedBy, Notes, Status)
                OUTPUT INSERTED.*
                VALUES (?, ?, ?, ?, ?, GETDATE(), ?, ?, ?)
            """, batch.ingredient_id, batch.quantity, batch.unit,
                 batch.batch_date, batch.expiration_date, batch.logged_by, batch.notes, batch_status)

            inserted = await cursor.fetchone()
            if not inserted:
                await conn.rollback()
                raise HTTPException(status_code=500, detail="Batch insert failed.")

            # update main ingredient table
            await cursor.execute("""
                UPDATE Ingredients 
                SET Amount = ?, BestBeforeDate = ?, ExpirationDate = ?, Status = ?, LastStatusChange = GETDATE()
                WHERE IngredientID = ?
            """, new_amount, batch.batch_date, batch.expiration_date, new_status, batch.ingredient_id)

            await conn.commit()

            if new_status == "Low Stock":
                logger.info(f"Ingredient {ingredient_name} is still at low stock after restock. Triggering notification.")
                await trigger_low_stock_notification(token)

            response_obj = IngredientBatchOut(
                batch_id=inserted.BatchID,
                ingredient_id=inserted.IngredientID,
                ingredient_name=ingredient_name, 
                quantity=inserted.Quantity,
                unit=inserted.Unit,
                batch_date=inserted.BatchDate,
                expiration_date=inserted.ExpirationDate,
                restock_date=inserted.RestockDate,
                logged_by=inserted.LoggedBy,
                notes=inserted.Notes,
                status=inserted.Status,
            )

        # log to blockchain
        try:
            blockchain_user_id = await get_user_id_from_token(token)
            restock_payload = {
                "action": "RESTOCK",
                "user_id": blockchain_user_id,
                "ItemType": "ingredient",
                "BatchID": inserted.BatchID,
                "ItemID": inserted.IngredientID,
                "Quantity": inserted.Quantity,
                "Unit": inserted.Unit,
                "BatchDate": str(inserted.BatchDate) if inserted.BatchDate else None,
                "RestockDate": str(inserted.RestockDate) if inserted.RestockDate else None,
                "LoggedBy": inserted.LoggedBy,
                "Notes": inserted.Notes,
                "Status": inserted.Status,
                "deduction_details": None,
                "old_values": {"quantity": current_amount, "status": (current_expiration and get_ingredient_status(current_amount, ingredient_unit, current_expiration)) or None},
                "new_values": {"quantity": float(new_amount), "status": new_status}
            }
            restock_payload = convert_decimal_to_float(restock_payload)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(BLOCKCHAIN_RESTOCK_URL, json=restock_payload, headers={"Authorization": f"Bearer {token}"})
                if resp.status_code in (200, 201):
                    try:
                        rx = resp.json()
                        tx_hash_for_restock = rx.get("tx_hash") or rx.get("txHash") or rx.get("tx")
                    except Exception:
                        tx_hash_for_restock = None
        except Exception as e:
            logger.error(f"Blockchain restock log failed (ingredient): {e}")

        # if auto waste was created (for expired replaced stock), log to blockchain-waste
        if auto_waste_inserted:
            try:
                blockchain_user_id = await get_user_id_from_token(token)
                waste_payload = {
                    "action": "AUTO WASTE",
                    "user_id": blockchain_user_id,
                    "WasteID": getattr(auto_waste_inserted, "WasteID", None),
                    "ItemType": "ingredient",
                    "ItemID": getattr(auto_waste_inserted, "ItemID", batch.ingredient_id),
                    "BatchID": getattr(auto_waste_inserted, "BatchID", None),
                    "Amount": getattr(auto_waste_inserted, "Amount", current_amount),
                    "Unit": getattr(auto_waste_inserted, "Unit", ingredient_unit),
                    "WasteReason": getattr(auto_waste_inserted, "WasteReason", "Expired stock"),
                    "WasteDate": str(getattr(auto_waste_inserted, "WasteDate", datetime.utcnow())),
                    "LoggedBy": getattr(auto_waste_inserted, "LoggedBy", batch.logged_by),
                    "Notes": getattr(auto_waste_inserted, "Notes", "Auto waste log for expired stock after restock"),
                    "Status": None,
                    "deduction_details": None,
                    "old_values": {"quantity": current_amount},
                    "new_values": None
                }
                waste_payload = convert_decimal_to_float(waste_payload)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(BLOCKCHAIN_WASTE_URL, json=waste_payload, headers={"Authorization": f"Bearer {token}"})
            except Exception as e:
                logger.error(f"Blockchain auto-waste log failed: {e}")

        # attach tx_hash to response where available (non-breaking)
        if tx_hash_for_restock:
            return {"batch": response_obj, "tx_hash": tx_hash_for_restock}
        return response_obj

    except Exception as e:
        await conn.rollback()
        logger.error(f"Error during restock: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to restock: {str(e)}")
    finally:
        conn.autocommit = True
        await conn.close()

# get all batches
@router.get("/", response_model=List[IngredientBatchOut])
async def get_all_batches(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "manager", "staff"])
    conn = await get_db_connection()
    try:
        async with conn.cursor() as cursor:
            # get main ingredient amount
            await cursor.execute("""
                SELECT 
                    ib.BatchID,
                    ib.IngredientID,
                    i.IngredientName,
                    ib.Quantity,
                    ib.Unit,
                    ib.BatchDate,
                    ib.ExpirationDate,
                    ib.RestockDate,
                    ib.LoggedBy,
                    ib.Notes,
                    ib.Status,
                    i.Amount AS IngredientAmount
                FROM IngredientBatches ib
                JOIN Ingredients i ON ib.IngredientID = i.IngredientID
            """)

            rows = await cursor.fetchall()
            result = []
            for row in rows:
                # status is calculated from ingredient amount and expiration
                new_status = get_batch_status(row.IngredientAmount, row.ExpirationDate)
                if new_status != row.Status:
                    await cursor.execute(
                        "UPDATE IngredientBatches SET Status = ? WHERE BatchID = ?",
                        new_status, row.BatchID
                    )
                    row.Status = new_status

                result.append(IngredientBatchOut(
                    batch_id=row.BatchID,
                    ingredient_id=row.IngredientID,
                    ingredient_name=row.IngredientName,
                    quantity=row.Quantity,           # original restock value
                    unit=row.Unit,
                    batch_date=row.BatchDate,
                    expiration_date=row.ExpirationDate,
                    restock_date=row.RestockDate,
                    logged_by=row.LoggedBy,
                    notes=row.Notes,
                    status=row.Status,
                ))
            await conn.commit()
            return result
    finally:
        await conn.close()

# get all batches by id
@router.get("/{ingredient_id}", response_model=List[IngredientBatchOut])
async def get_batches_for_ingredient(ingredient_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "manager", "staff"])
    conn = await get_db_connection()
    try:
        async with conn.cursor() as cursor:
            # get main ingredient amount
            await cursor.execute("SELECT Amount FROM Ingredients WHERE IngredientID = ?", ingredient_id)
            ing_row = await cursor.fetchone()
            ingredient_amount = ing_row.Amount if ing_row else 0

            await cursor.execute("""
                SELECT BatchID, IngredientID, Quantity, Unit, BatchDate, ExpirationDate, RestockDate, LoggedBy, Notes, Status
                FROM IngredientBatches
                WHERE IngredientID = ?
            """, ingredient_id)

            rows = await cursor.fetchall()
            result = []
            for row in rows:
                new_status = get_batch_status(ingredient_amount, row.ExpirationDate)
                if new_status != row.Status:
                    await cursor.execute(
                        "UPDATE IngredientBatches SET Status = ? WHERE BatchID = ?",
                        new_status, row.BatchID
                    )
                    row.Status = new_status
                result.append(IngredientBatchOut(
                    batch_id=row.BatchID,
                    ingredient_id=row.IngredientID,
                    ingredient_name=row.IngredientName,
                    quantity=row.Quantity,   # original restock value
                    unit=row.Unit,
                    batch_date=row.BatchDate,
                    expiration_date=row.ExpirationDate,
                    restock_date=row.RestockDate,
                    logged_by=row.LoggedBy,
                    notes=row.Notes,
                    status=row.Status,
                ))
            await conn.commit()
            return result
    finally:
        await conn.close()

# update restock
@router.put("/{batch_id}", response_model=Union[IngredientBatchOut, Dict])
async def update_batch(batch_id: int, data: IngredientBatchUpdate, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "manager", "staff"])
    conn = await get_db_connection()
    tx_hash_for_update: Optional[str] = None
    try:
        async with conn.cursor() as cursor:
            # get old data
            await cursor.execute("SELECT Quantity, IngredientID FROM IngredientBatches WHERE BatchID = ?", batch_id)
            old_row = await cursor.fetchone()
            if not old_row:
                raise HTTPException(status_code=404, detail="Batch not found.")
            old_quantity = float(old_row.Quantity)
            ingredient_id = old_row.IngredientID

            updates = []
            values = []

            field_to_column = {
                "quantity": "Quantity",
                "unit": "Unit",
                "batch_date": "BatchDate",
                "expiration_date": "ExpirationDate",
                "logged_by": "LoggedBy",
                "notes": "Notes"
            }

            for key, value in data.dict(exclude_unset=True).items():
                column = field_to_column.get(key)
                if column:
                    updates.append(f"{column} = ?")
                    values.append(value)

            if not updates:
                raise HTTPException(status_code=400, detail="No fields to update.")

            values.append(batch_id)

            # perform update
            await cursor.execute(f"""
                UPDATE IngredientBatches SET {', '.join(updates)} WHERE BatchID = ?
            """, *values)

            # sync total stock if quantity changed
            if "quantity" in data.dict(exclude_unset=True):
                diff = float(data.quantity) - old_quantity
                await cursor.execute("""
                    UPDATE Ingredients SET Amount = Amount + ? WHERE IngredientID = ?
                """, diff, ingredient_id)

            # re-fetch and apply status rules
            await cursor.execute("SELECT * FROM IngredientBatches WHERE BatchID = ?", batch_id)
            row = await cursor.fetchone()
            new_status = row.Status  # default
            if row.ExpirationDate <= date.today():
                new_status = "Expired"
            elif row.Quantity == 0:
                new_status = "Used"
            else:
                new_status = "Available"

            # update status if needed
            await cursor.execute(
                "UPDATE IngredientBatches SET Status = ? WHERE BatchID = ?",
                new_status, batch_id
            )

            await conn.commit()

            # log to blockchain
            try:
                blockchain_user_id = await get_user_id_from_token(token)
                async with conn.cursor() as _c:
                    await _c.execute("SELECT * FROM IngredientBatches WHERE BatchID = ?", batch_id)
                    updated = await _c.fetchone()

                block_payload = {
                    "action": "UPDATE",
                    "user_id": blockchain_user_id,
                    "ItemType": "ingredient",
                    "BatchID": batch_id,
                    "ItemID": updated.IngredientID,
                    "Quantity": updated.Quantity,
                    "Unit": updated.Unit,
                    "BatchDate": str(updated.BatchDate) if updated.BatchDate else None,
                    "RestockDate": str(updated.RestockDate) if updated.RestockDate else None,
                    "LoggedBy": updated.LoggedBy,
                    "Notes": updated.Notes,
                    "Status": updated.Status,
                    "deduction_details": None,
                    "old_values": {"quantity": old_quantity},
                    "new_values": {"quantity": updated.Quantity, "status": updated.Status}
                }
                block_payload = convert_decimal_to_float(block_payload)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(BLOCKCHAIN_RESTOCK_URL, json=block_payload, headers={"Authorization": f"Bearer {token}"})
                    if resp.status_code in (200, 201):
                        try:
                            rx = resp.json()
                            tx_hash_for_update = rx.get("tx_hash") or rx.get("txHash") or rx.get("tx")
                        except Exception:
                            tx_hash_for_update = None
            except Exception as e:
                logger.error(f"Blockchain restock log failed: {e}")

            return_obj = IngredientBatchOut(
                batch_id=row.BatchID,
                ingredient_id=row.IngredientID,
                ingredient_name=row.IngredientName,
                quantity=row.Quantity,
                unit=row.Unit,
                batch_date=row.BatchDate,
                expiration_date=row.ExpirationDate,
                restock_date=row.RestockDate,
                logged_by=row.LoggedBy,
                notes=row.Notes,
                status=new_status,
            )

            if tx_hash_for_update:
                return {"batch": return_obj, "tx_hash": tx_hash_for_update}
            return return_obj
    finally:
        await conn.close()