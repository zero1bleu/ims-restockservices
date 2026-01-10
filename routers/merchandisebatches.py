from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import List, Optional, Union
from datetime import date, datetime
import httpx
from database import get_db_connection
import decimal as _decimal
import logging

logger = logging.getLogger(__name__)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="https://authservices-npr8.onrender.com/auth/token")
router = APIRouter()

BLOCKCHAIN_URL = "https://ims-blockchain.onrender.com/blockchain/restock"

# helper to get user id from token
async def get_user_id_from_token(token: str) -> int:
    USER_SERVICE_ME_URL = "https://authservices-npr8.onrender.com/auth/users/me"
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
    if isinstance(obj, _decimal.Decimal):
        return float(obj)
    return obj

def get_batch_status(merchandise_quantity: float) -> str:
    if merchandise_quantity <= 0:
        return "Used"
    return "Available"

# auth validation
async def validate_token_and_roles(token: str, allowed_roles: List[str]):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://authservices-npr8.onrender.com/auth/users/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Auth failed")
    if response.json().get("userRole") not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")

# models
class MerchandiseBatchCreate(BaseModel):
    merchandise_id: int
    quantity: float
    unit: str
    batch_date: date
    logged_by: str
    notes: Optional[str] = None

class MerchandiseBatchUpdate(BaseModel):
    quantity: Optional[float]
    unit: Optional[str]
    batch_date: Optional[date]
    logged_by: Optional[str]
    notes: Optional[str]

class MerchandiseBatchOut(BaseModel):
    batch_id: int
    merchandise_id: int
    merchandise_name: str
    quantity: float
    unit: str
    batch_date: date
    restock_date: datetime
    logged_by: str
    notes: Optional[str]
    status: str

# restock merchandise
@router.post("/", response_model=Union[MerchandiseBatchOut, dict])
async def create_batch(batch: MerchandiseBatchCreate, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "manager", "staff"])
    conn = await get_db_connection()
    tx_hash_for_restock: Optional[str] = None
    try:
        async with conn.cursor() as cursor:
            status = "Available"
            if batch.quantity == 0:
                status = "Used"

            # get old values
            await cursor.execute("SELECT MerchandiseName, MerchandiseQuantity, Status FROM Merchandise WHERE MerchandiseID = ?", batch.merchandise_id)
            merch_row = await cursor.fetchone()
            if not merch_row:
                raise HTTPException(status_code=404, detail="Merchandise not found")
            old_quantity = merch_row.MerchandiseQuantity
            old_status = merch_row.Status
            merchandise_name = merch_row.MerchandiseName

            # insert batch
            await cursor.execute("""
                INSERT INTO MerchandiseBatches 
                (MerchandiseID, Quantity, Unit, BatchDate, RestockDate, LoggedBy, Notes, Status)
                OUTPUT 
                    INSERTED.BatchID,
                    INSERTED.MerchandiseID,
                    INSERTED.Quantity,
                    INSERTED.Unit,
                    INSERTED.BatchDate,
                    INSERTED.RestockDate,
                    INSERTED.LoggedBy,
                    INSERTED.Notes,
                    INSERTED.Status
                VALUES (?, ?, ?, ?, GETDATE(), ?, ?, ?)
            """, batch.merchandise_id, batch.quantity, batch.unit, batch.batch_date, batch.logged_by, batch.notes, status)
            inserted = await cursor.fetchone()
            if not inserted:
                raise HTTPException(status_code=500, detail="Batch insert failed.")

            # fetch merchandise name
            await cursor.execute("SELECT MerchandiseName FROM Merchandise WHERE MerchandiseID = ?", inserted.MerchandiseID)
            merchandise_row = await cursor.fetchone()
            if not merchandise_row:
                raise HTTPException(status_code=404, detail="Merchandise not found")

            merchandise_name = merchandise_row.MerchandiseName

            # update stock and date in main table
            await cursor.execute("""
                UPDATE Merchandise 
                SET MerchandiseQuantity = MerchandiseQuantity + ?, MerchandiseDateAdded = ?
                WHERE MerchandiseID = ?
            """, batch.quantity, batch.batch_date, batch.merchandise_id)

            await conn.commit()

            response_obj = MerchandiseBatchOut(
                batch_id=inserted.BatchID,
                merchandise_id=inserted.MerchandiseID,
                merchandise_name=merchandise_name,
                quantity=inserted.Quantity,
                unit=inserted.Unit,
                batch_date=inserted.BatchDate,
                restock_date=inserted.RestockDate,
                logged_by=inserted.LoggedBy,
                notes=inserted.Notes,
                status=inserted.Status,
            )

        # log to blockchain
        try:
            blockchain_user_id = await get_user_id_from_token(token)
            block_payload = {
                "action": "RESTOCK",
                "user_id": blockchain_user_id,
                "ItemType": "merchandise",
                "BatchID": inserted.BatchID,
                "ItemID": inserted.MerchandiseID,
                "Quantity": inserted.Quantity,
                "Unit": inserted.Unit,
                "BatchDate": str(inserted.BatchDate) if inserted.BatchDate else None,
                "RestockDate": str(inserted.RestockDate) if inserted.RestockDate else None,
                "LoggedBy": inserted.LoggedBy,
                "Notes": inserted.Notes,
                "Status": inserted.Status,
                "deduction_details": None,
                "old_values": {"quantity": old_quantity, "status": old_status},
                "new_values": {"quantity": inserted.Quantity, "status": inserted.Status}
            }
            block_payload = convert_decimal_to_float(block_payload)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(BLOCKCHAIN_URL, json=block_payload, headers={"Authorization": f"Bearer {token}"})
                if resp.status_code in (200, 201):
                    try:
                        rx = resp.json()
                        tx_hash_for_restock = rx.get("tx_hash") or rx.get("txHash") or rx.get("tx")
                    except Exception:
                        tx_hash_for_restock = None
        except Exception as e:
            logger.error(f"Blockchain restock log failed: {e}")

        # return response, include tx_hash if available (non-breaking)
        if tx_hash_for_restock:
            return {"batch": response_obj, "tx_hash": tx_hash_for_restock}
        return response_obj
    finally:
        await conn.close()

# get all batches
@router.get("/", response_model=List[MerchandiseBatchOut])
async def get_all_merchandise_batches(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "manager", "staff"])
    conn = await get_db_connection()
    try:
        async with conn.cursor() as cursor:
            # get main current amount
            await cursor.execute("""
                SELECT 
                    ib.BatchID,
                    ib.MerchandiseID,
                    i.MerchandiseName,
                    ib.Quantity,
                    ib.Unit,
                    ib.BatchDate,
                    ib.RestockDate,
                    ib.LoggedBy,
                    ib.Notes,
                    ib.Status,
                    i.MerchandiseQuantity
                FROM MerchandiseBatches ib
                JOIN Merchandise i ON ib.MerchandiseID = i.MerchandiseID
            """)
            rows = await cursor.fetchall()

            result = []
            for row in rows:
                new_status = get_batch_status(row.MerchandiseQuantity)
                if new_status != row.Status:
                    await cursor.execute(
                        "UPDATE MerchandiseBatches SET Status = ? WHERE BatchID = ?",
                        new_status, row.BatchID
                    )
                    row.Status = new_status
                result.append(MerchandiseBatchOut(
                    batch_id=row.BatchID,
                    merchandise_id=row.MerchandiseID,
                    merchandise_name=row.MerchandiseName,
                    quantity=row.Quantity, # original restock value
                    unit=row.Unit,
                    batch_date=row.BatchDate,
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
@router.get("/{merchandise_id}", response_model=List[MerchandiseBatchOut])
async def get_batches(merchandise_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "manager", "staff"])
    conn = await get_db_connection()
    try:
        async with conn.cursor() as cursor:
            # get main current amount
            await cursor.execute("SELECT MerchandiseQuantity FROM Merchandise WHERE MerchandiseID = ?", merchandise_id)
            merch_row = await cursor.fetchone()
            merchandise_quantity = merch_row.MerchandiseQuantity if merch_row else 0

            await cursor.execute("""
                SELECT BatchID, MerchandiseID, Quantity, Unit, BatchDate, RestockDate, LoggedBy, Notes, Status
                FROM MerchandiseBatches WHERE MerchandiseID = ?
            """, merchandise_id)
            rows = await cursor.fetchall()

            result = []
            for row in rows:
                new_status = get_batch_status(merchandise_quantity)
                if new_status != row.Status:
                    await cursor.execute(
                        "UPDATE MerchandiseBatches SET Status = ? WHERE BatchID = ?",
                        new_status, row.BatchID
                    )
                    row.Status = new_status  # reflect in output

                result.append(MerchandiseBatchOut(
                    batch_id=row.BatchID,
                    merchandise_id=row.MerchandiseID,
                    merchandise_name=row.MerchandiseName,
                    quantity=row.Quantity,
                    unit=row.Unit,
                    batch_date=row.BatchDate,
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
@router.put("/{batch_id}", response_model=Union[MerchandiseBatchOut, dict])
async def update_batch(batch_id: int, data: MerchandiseBatchUpdate, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "manager", "staff"])
    conn = await get_db_connection()
    tx_hash_for_update: Optional[str] = None
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT Quantity, MerchandiseID FROM MerchandiseBatches WHERE BatchID = ?", batch_id)
            old = await cursor.fetchone()
            if not old:
                raise HTTPException(status_code=404, detail="Batch not found")
            updates, values = [], []
            map_col = {
                "quantity": "Quantity",
                "unit": "Unit",
                "batch_date": "BatchDate",
                "logged_by": "LoggedBy",
                "notes": "Notes"
            }
            for k, v in data.dict(exclude_unset=True).items():
                if k not in map_col:
                    continue
                updates.append(f"{map_col[k]} = ?")
                values.append(v)
            if not updates:
                raise HTTPException(status_code=400, detail="Nothing to update")
            values.append(batch_id)
            await cursor.execute(f"UPDATE MerchandiseBatches SET {', '.join(updates)} WHERE BatchID = ?", *values)
            if "quantity" in data.dict(exclude_unset=True):
                diff = float(data.quantity) - float(old.Quantity)
                await cursor.execute("UPDATE Merchandise SET MerchandiseQuantity = MerchandiseQuantity + ? WHERE MerchandiseID = ?", diff, old.MerchandiseID)
            await cursor.execute("""
                SELECT BatchID, MerchandiseID, Quantity, Unit, BatchDate, RestockDate, LoggedBy, Notes, Status
                FROM MerchandiseBatches WHERE BatchID = ?
            """, batch_id)
            updated = await cursor.fetchone()
            if not updated:
                raise HTTPException(status_code=404, detail="Batch not found after update.")
            
            # update status if needed
            new_status = updated.Status
            if updated.Quantity == 0:
                new_status = "Used"
            else:
                new_status = "Available"
            if new_status != updated.Status:
                await cursor.execute(
                    "UPDATE MerchandiseBatches SET Status = ? WHERE BatchID = ?",
                    new_status, batch_id
                )
                updated.Status = new_status

            await conn.commit()

            # log to blockchain
            try:
                blockchain_user_id = await get_user_id_from_token(token)
                async with conn.cursor() as _c:
                    await _c.execute("SELECT * FROM MerchandiseBatches WHERE BatchID = ?", batch_id)
                    updated_row = await _c.fetchone()

                block_payload = {
                    "action": "UPDATE",
                    "user_id": blockchain_user_id,
                    "ItemType": "merchandise",
                    "BatchID": batch_id,
                    "ItemID": updated_row.MerchandiseID,
                    "Quantity": updated_row.Quantity,
                    "Unit": updated_row.Unit,
                    "BatchDate": str(updated_row.BatchDate) if updated_row.BatchDate else None,
                    "RestockDate": str(updated_row.RestockDate) if updated_row.RestockDate else None,
                    "LoggedBy": updated_row.LoggedBy,
                    "Notes": updated_row.Notes,
                    "Status": updated_row.Status,
                    "deduction_details": None,
                    "old_values": {"quantity": old.Quantity},
                    "new_values": {"quantity": updated_row.Quantity, "status": updated_row.Status}
                }
                block_payload = convert_decimal_to_float(block_payload)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(BLOCKCHAIN_URL, json=block_payload, headers={"Authorization": f"Bearer {token}"})
                    if resp.status_code in (200, 201):
                        try:
                            rx = resp.json()
                            tx_hash_for_update = rx.get("tx_hash") or rx.get("txHash") or rx.get("tx")
                        except Exception:
                            tx_hash_for_update = None
            except Exception as e:
                logger.error(f"Blockchain restock log failed: {e}")

            return_obj = MerchandiseBatchOut(
                batch_id=updated.BatchID,
                merchandise_id=updated.MerchandiseID,
                merchandise_name=updated.MerchandiseName,
                quantity=updated.Quantity,
                unit=updated.Unit,
                batch_date=updated.BatchDate,
                restock_date=updated.RestockDate,
                logged_by=updated.LoggedBy,
                notes=updated.Notes,
                status=updated.Status,
            )

            if tx_hash_for_update:
                return {"batch": return_obj, "tx_hash": tx_hash_for_update}
            return return_obj
    finally:
        await conn.close()