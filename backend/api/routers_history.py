from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel
from datetime import datetime

from db import database, models
from core import auth

router = APIRouter(prefix="/history", tags=["history"])

class ChatHistoryBase(BaseModel):
    title: str

class ChatHistoryResponse(ChatHistoryBase):
    id: int
    user_id: int
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class ChatMessageBase(BaseModel):
    role: str
    text: str

class ChatMessageResponse(ChatMessageBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True

class ChatHistoryDetailResponse(ChatHistoryResponse):
    messages: List[ChatMessageResponse] = []

@router.get("/", response_model=List[ChatHistoryResponse])
def get_histories(db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.get_current_user)):
    return db.query(models.ChatHistory).filter(models.ChatHistory.user_id == current_user.id, models.ChatHistory.status == "active").order_by(models.ChatHistory.updated_at.desc()).all()

@router.get("/archived", response_model=List[ChatHistoryResponse])
def get_archived_histories(db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.get_current_user)):
    return db.query(models.ChatHistory).filter(models.ChatHistory.user_id == current_user.id, models.ChatHistory.status == "archived").order_by(models.ChatHistory.updated_at.desc()).all()

@router.post("/", response_model=ChatHistoryResponse)
def create_history(history: ChatHistoryBase, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.get_current_user)):
    new_history = models.ChatHistory(title=history.title, user_id=current_user.id)
    db.add(new_history)
    db.commit()
    db.refresh(new_history)
    return new_history

@router.get("/{history_id}", response_model=ChatHistoryDetailResponse)
def get_history(history_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.get_current_user)):
    history = db.query(models.ChatHistory).filter(models.ChatHistory.id == history_id, models.ChatHistory.user_id == current_user.id).first()
    if not history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    return history

@router.put("/{history_id}", response_model=ChatHistoryResponse)
def rename_history(history_id: int, history: ChatHistoryBase, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.get_current_user)):
    db_history = db.query(models.ChatHistory).filter(models.ChatHistory.id == history_id, models.ChatHistory.user_id == current_user.id).first()
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.title = history.title
    db.commit()
    db.refresh(db_history)
    return db_history

@router.patch("/{history_id}/archive")
def archive_history(history_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.get_current_user)):
    db_history = db.query(models.ChatHistory).filter(models.ChatHistory.id == history_id, models.ChatHistory.user_id == current_user.id).first()
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.status = "archived"
    db.commit()
    return {"status": "success"}

@router.patch("/{history_id}/unarchive")
def unarchive_history(history_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.get_current_user)):
    db_history = db.query(models.ChatHistory).filter(models.ChatHistory.id == history_id, models.ChatHistory.user_id == current_user.id).first()
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.status = "active"
    db.commit()
    return {"status": "success"}

@router.delete("/{history_id}")
def delete_history(history_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(auth.get_current_user)):
    db_history = db.query(models.ChatHistory).filter(models.ChatHistory.id == history_id, models.ChatHistory.user_id == current_user.id).first()
    if not db_history:
        raise HTTPException(status_code=404, detail="Chat history not found")
    
    db_history.status = "deleted" # Soft delete
    db.commit()
    return {"status": "success"}
