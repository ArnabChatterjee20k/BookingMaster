# Goals
* User can authenticate themselves + create orgs and add members to them
* Searching, viewing, booking with consistency
* Admins can edit events dynamically
* Viewers can view
* Auth in place
* Read heavy

# DB models
## Users
id -> primary , int, auto increment
uid -> uuid -> in case of required to share profile
email -> unique
password -> currently storing as normal value only

## Organisation
id -> primary , int, auto increment
uid -> uuid -> in case of required to share profile
name
created_at

## Membership
id
uid
org_uid
user_uid
role
unique(org_uid , user_uid)

## Venues
id
uid
name
creator_user_uid
org_uid
location -> [coords] -> spatial points -> spatial indexing

## Events
id
uid
name
org_id,
performer_id -> user_id only
venue_id
starts_at -> utc
ends_at -> utc

## Tickets Tier
id
uid
tier_name
price
event_id
available -> inventory per tier

## Bookings
> a single payment is always made with the summed up money of all the tickets
id
uid -> idempotent key
amount
status
event_id
created_at
expires_at

## Tickets
id
uid
booking_id
ticket_tier_id
event_id
status