import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Busroute, Trip, Reservation

app = FastAPI(title="Cameroon Bus Booking API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CITIES = [
    "Yaoundé", "Douala", "Bafoussam", "Bamenda", "Garoua", "Maroua",
    "Ngaoundéré", "Bertoua", "Ebolowa", "Buea", "Kumba", "Limbe", "Kribi"
]

SEAT_COUNT = 68
LOCK_DURATION_MINUTES = 10

@app.get("/")
def root():
    return {"message": "Cameroon Bus Booking API"}

@app.get("/api/cities")
def get_cities():
    return {"cities": CITIES}

@app.post("/api/routes", response_model=dict)
def create_route(route: Busroute):
    route_id = create_document("busroute", route)
    return {"id": route_id}

class SearchPayload(BaseModel):
    depart: str
    arrivee: str
    date_voyage: str  # YYYY-MM-DD

@app.post("/api/search")
def search_or_create_trip(payload: SearchPayload):
    if payload.depart not in CITIES or payload.arrivee not in CITIES:
        raise HTTPException(status_code=400, detail="Villes non supportées")

    # Find existing active route price or default
    route = db["busroute"].find_one({"depart": payload.depart, "arrivee": payload.arrivee, "actif": True})
    prix = route.get("prix", 8000) if route else 8000

    # Check if trip exists for that date
    trip = db["trip"].find_one({
        "depart": payload.depart,
        "arrivee": payload.arrivee,
        "date_voyage": payload.date_voyage,
    })
    if not trip:
        trip_model = Trip(
            route_id=str(route.get("_id")) if route else "",
            depart=payload.depart,
            arrivee=payload.arrivee,
            date_voyage=payload.date_voyage,
            prix=prix,
            capacite=SEAT_COUNT,
        )
        trip_id = create_document("trip", trip_model)
        trip = db["trip"].find_one({"_id": ObjectId(trip_id)})

    # Cleanup expired locks
    _cleanup_expired_locks(trip)

    return _serialize_trip(trip)

@app.get("/api/trip/{trip_id}")
def get_trip(trip_id: str):
    trip = db["trip"].find_one({"_id": ObjectId(trip_id)})
    if not trip:
        raise HTTPException(404, "Trajet introuvable")
    _cleanup_expired_locks(trip)
    return _serialize_trip(trip)

class LockPayload(BaseModel):
    seats: List[int]

@app.post("/api/trip/{trip_id}/lock")
def lock_seats(trip_id: str, payload: LockPayload):
    trip = db["trip"].find_one({"_id": ObjectId(trip_id)})
    if not trip:
        raise HTTPException(404, "Trajet introuvable")
    _cleanup_expired_locks(trip)

    booked = set(trip.get("booked_seats", []))
    locked_list = trip.get("locked_seats", [])
    now = datetime.now(timezone.utc)

    # Validate seats
    for s in payload.seats:
        if s < 1 or s > SEAT_COUNT:
            raise HTTPException(400, f"Siège invalide: {s}")
        if s in booked:
            raise HTTPException(409, f"Siège {s} déjà réservé")
        if any(l["seat"] == s for l in locked_list if l.get("expires") and datetime.fromisoformat(l["expires"]) > now):
            raise HTTPException(409, f"Siège {s} en cours de sélection par un autre utilisateur")

    new_locks = [{"seat": s, "expires": (now + timedelta(minutes=LOCK_DURATION_MINUTES)).isoformat()} for s in payload.seats]
    db["trip"].update_one({"_id": ObjectId(trip_id)}, {"$push": {"locked_seats": {"$each": new_locks}}})

    trip = db["trip"].find_one({"_id": ObjectId(trip_id)})
    return _serialize_trip(trip)

class ReservationPayload(BaseModel):
    seats: List[int]
    nom_complet: str
    telephone: str
    email: Optional[str] = None

@app.post("/api/trip/{trip_id}/reserve")
def create_reservation(trip_id: str, payload: ReservationPayload):
    trip = db["trip"].find_one({"_id": ObjectId(trip_id)})
    if not trip:
        raise HTTPException(404, "Trajet introuvable")
    _cleanup_expired_locks(trip)

    booked = set(trip.get("booked_seats", []))
    locked_list = trip.get("locked_seats", [])
    now = datetime.now(timezone.utc)

    # Verify seats are currently locked (by anyone) and not booked
    for s in payload.seats:
        if s in booked:
            raise HTTPException(409, f"Siège {s} déjà réservé")
        lock_ok = any(l["seat"] == s and datetime.fromisoformat(l["expires"]) > now for l in locked_list)
        if not lock_ok:
            raise HTTPException(409, f"Sélection expirée pour le siège {s}, veuillez re-sélectionner")

    total = len(payload.seats) * int(trip.get("prix", 8000))

    res_model = Reservation(
        trip_id=trip_id,
        seats=payload.seats,
        montant_total=total,
        statut="pending",
        nom_complet=payload.nom_complet,
        telephone=payload.telephone,
        email=payload.email,
    )
    res_id = create_document("reservation", res_model)
    reservation = db["reservation"].find_one({"_id": ObjectId(res_id)})

    return {"reservation_id": res_id, "montant_total": total}

class PaypalCapturePayload(BaseModel):
    order_id: str

@app.post("/api/payment/paypal/capture/{reservation_id}")
def paypal_capture(reservation_id: str, payload: PaypalCapturePayload):
    # In real-life, verify PayPal order via PayPal API webhooks or capture API.
    # Here we assume frontend captures the order and sends order_id as proof.
    reservation = db["reservation"].find_one({"_id": ObjectId(reservation_id)})
    if not reservation:
        raise HTTPException(404, "Réservation introuvable")

    if reservation.get("statut") == "paid":
        return {"status": "already_paid"}

    trip = db["trip"].find_one({"_id": ObjectId(reservation["trip_id"])})
    if not trip:
        raise HTTPException(404, "Trajet introuvable")

    # Mark as paid and allocate seats
    seats = reservation.get("seats", [])
    db["trip"].update_one({"_id": ObjectId(trip["_id"])}, {"$addToSet": {"booked_seats": {"$each": seats}}})

    ticket_no = f"CBB-{str(reservation_id)[-6:].upper()}-{int(datetime.now().timestamp())}"
    db["reservation"].update_one(
        {"_id": ObjectId(reservation_id)},
        {"$set": {"statut": "paid", "paypal_order_id": payload.order_id, "ticket_no": ticket_no, "paid_at": datetime.now(timezone.utc).isoformat()}}
    )

    # Remove locks for these seats
    db["trip"].update_one({"_id": ObjectId(trip["_id"])}, {"$pull": {"locked_seats": {"seat": {"$in": seats}}}})

    reservation = db["reservation"].find_one({"_id": ObjectId(reservation_id)})
    return _serialize_res(reservation)

@app.get("/api/reservation/{reservation_id}")
def get_reservation(reservation_id: str):
    res = db["reservation"].find_one({"_id": ObjectId(reservation_id)})
    if not res:
        raise HTTPException(404, "Réservation introuvable")
    return _serialize_res(res)

@app.get("/api/ticket/{reservation_id}/qrcode")
def ticket_qrcode(reservation_id: str):
    import qrcode
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    res = db["reservation"].find_one({"_id": ObjectId(reservation_id)})
    if not res:
        raise HTTPException(404, "Réservation introuvable")

    data = f"Cameroon Bus Booking|{res.get('ticket_no')}|{res.get('trip_id')}|{','.join(map(str, res.get('seats', [])))}|{res.get('montant_total')}"
    img = qrcode.make(data)
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

@app.get("/api/ticket/{reservation_id}/pdf")
def ticket_pdf(reservation_id: str):
    from fastapi.responses import StreamingResponse
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    res = db["reservation"].find_one({"_id": ObjectId(reservation_id)})
    if not res:
        raise HTTPException(404, "Réservation introuvable")

    trip = db["trip"].find_one({"_id": ObjectId(res.get('trip_id'))})

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "Cameroon Bus Booking - Billet")
    c.setFont("Helvetica", 12)
    c.drawString(50, height - 90, f"Numéro de réservation: {res.get('ticket_no')}")
    c.drawString(50, height - 110, f"Trajet: {trip.get('depart')} → {trip.get('arrivee')}")
    c.drawString(50, height - 130, f"Date: {trip.get('date_voyage')}")
    c.drawString(50, height - 150, f"Sièges: {', '.join(map(str, res.get('seats', [])))}")
    c.drawString(50, height - 170, f"Total: {res.get('montant_total')} FCFA")
    c.drawString(50, height - 190, f"Nom: {res.get('nom_complet')}")
    c.drawString(50, height - 210, f"Téléphone: {res.get('telephone')}")

    c.showPage()
    c.save()

    buffer.seek(0)
    return StreamingResponse(buffer, media_type='application/pdf', headers={
        "Content-Disposition": f"attachment; filename=billet_{res.get('ticket_no')}.pdf"
    })


# Helper functions

def _cleanup_expired_locks(trip: dict):
    now = datetime.now(timezone.utc)
    locked_list = trip.get("locked_seats", [])
    valid = [l for l in locked_list if l.get("expires") and datetime.fromisoformat(l["expires"]) > now]
    if len(valid) != len(locked_list):
        db["trip"].update_one({"_id": ObjectId(trip["_id"])}, {"$set": {"locked_seats": valid}})
    trip["locked_seats"] = valid


def _serialize_trip(trip: dict):
    return {
        "id": str(trip.get("_id")),
        "depart": trip.get("depart"),
        "arrivee": trip.get("arrivee"),
        "date_voyage": trip.get("date_voyage"),
        "prix": int(trip.get("prix", 8000)),
        "capacite": int(trip.get("capacite", SEAT_COUNT)),
        "booked_seats": trip.get("booked_seats", []),
        "locked_seats": trip.get("locked_seats", []),
    }


def _serialize_res(res: dict):
    return {
        "id": str(res.get("_id")),
        "trip_id": res.get("trip_id"),
        "seats": res.get("seats", []),
        "montant_total": res.get("montant_total"),
        "statut": res.get("statut"),
        "nom_complet": res.get("nom_complet"),
        "telephone": res.get("telephone"),
        "email": res.get("email"),
        "paypal_order_id": res.get("paypal_order_id"),
        "ticket_no": res.get("ticket_no"),
        "paid_at": res.get("paid_at"),
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
