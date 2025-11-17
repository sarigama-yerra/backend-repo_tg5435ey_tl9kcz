"""
Database Schemas for Cameroon Bus Booking

Each Pydantic model represents a collection in MongoDB.
Collection name is the lowercase of the class name.
"""

from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class Busroute(BaseModel):
    depart: str = Field(..., description="Ville de départ")
    arrivee: str = Field(..., description="Ville d'arrivée")
    prix: int = Field(..., ge=0, description="Prix par place en FCFA")
    actif: bool = Field(True, description="Route active")

class Trip(BaseModel):
    route_id: str = Field(..., description="ID de la route associée")
    depart: str = Field(..., description="Ville de départ")
    arrivee: str = Field(..., description="Ville d'arrivée")
    date_voyage: str = Field(..., description="Date du voyage au format YYYY-MM-DD")
    prix: int = Field(..., ge=0, description="Prix par place en FCFA")
    capacite: int = Field(68, description="Nombre total de places")
    booked_seats: List[int] = Field(default_factory=list, description="Sièges réservés (payés)")
    locked_seats: List[dict] = Field(default_factory=list, description="Sièges verrouillés temporairement: {seat:int, expires:datetime}")

class Reservation(BaseModel):
    trip_id: str = Field(..., description="ID du trajet")
    seats: List[int] = Field(..., description="Sièges réservés")
    montant_total: int = Field(..., ge=0, description="Montant total en FCFA")
    statut: str = Field("pending", description="pending, paid, cancelled, expired")
    nom_complet: str = Field(..., description="Nom complet du voyageur")
    telephone: str = Field(..., description="Numéro WhatsApp")
    email: Optional[str] = Field(None, description="Email")
    paypal_order_id: Optional[str] = Field(None, description="Order ID PayPal")
    ticket_no: Optional[str] = Field(None, description="Numéro de réservation")
    paid_at: Optional[datetime] = Field(None, description="Date de paiement")
