#!/bin/sh
# infra/backup/entrypoint.sh
# Sauvegarde et restauration d'ANITCHE (docs/SAUVEGARDES.md), dans le
# conteneur à usage unique « backup » de docker-compose.prod.yml, lancé par
# infra/scripts/backup.sh et infra/scripts/restore.sh :
#   init         crée le dépôt restic (une seule fois, à la main) ;
#   sauvegarder [--controle-approfondi]
#                base, puis médias, rétention et contrôle du dépôt ;
#   instantanes  liste les instantanés ;
#   restaurer [--base NOM] [--medias DOSSIER]
#             [--instantane-base ID] [--instantane-medias ID]
#                restaure dans une base vide et un dossier vide, puis vérifie.
# Variables : RESTIC_REPOSITORY, RESTIC_PASSWORD, AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION (facultative), RESTIC_CACHE_DIR,
# PGHOST (db par défaut), PGPASSWORD, DB_NAME, DB_USER. Aucune valeur n'est
# jamais affichée.

set -eu
set -o pipefail

# Hôte fixe : le nom d'un conteneur « run --rm » change à chaque exécution,
# or restic regroupe les instantanés par hôte et chemin pour la rétention.
HOTE=anitche
MEDIAS=/sauvegarde/media
FICHIER_DUMP=anitche.dump
# Contrôle approfondi : une part différente des données relue chaque
# semaine ; tout le dépôt est relu en PARTS_CONTROLE semaines.
PARTS_CONTROLE=8
# Tables dont restaurer affiche le nombre de lignes.
TABLES_COMPTEES='utilisateurs_utilisateur vendeurs_boutique catalogue_produit commandes_commande paiements_paiement utilisateurs_documentkyc'
# Chemins des fichiers référencés par la base (champs FileField et
# ImageField des modèles, relatifs à MEDIA_ROOT). Tout nouveau champ
# fichier s'ajoute ici, sinon restaurer ne vérifie pas ses fichiers.
REQUETE_FICHIERS="
SELECT piece_identite_recto FROM utilisateurs_documentkyc WHERE piece_identite_recto <> ''
UNION ALL SELECT piece_identite_verso FROM utilisateurs_documentkyc WHERE piece_identite_verso <> ''
UNION ALL SELECT selfie FROM utilisateurs_documentkyc WHERE selfie <> ''
UNION ALL SELECT image FROM catalogue_imageproduit WHERE image <> ''
UNION ALL SELECT image FROM catalogue_categorie WHERE image <> ''
UNION ALL SELECT logo FROM vendeurs_boutique WHERE logo <> ''
UNION ALL SELECT banniere FROM vendeurs_boutique WHERE banniere <> ''
UNION ALL SELECT file FROM support_ticketattachment WHERE file <> ''
UNION ALL SELECT image FROM retours_photoretour WHERE image <> ''"

: "${PGHOST:=db}"
export PGHOST

usage() {
    cat >&2 <<'FIN'
Usage : sauvegarde init
        sauvegarde sauvegarder [--controle-approfondi]
        sauvegarde instantanes
        sauvegarde restaurer [--base NOM] [--medias DOSSIER]
                             [--instantane-base ID] [--instantane-medias ID]
FIN
    exit 2
}

journal() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

refus() {
    journal "REFUS : $*" >&2
    exit 1
}

# Une étape : $1 est son nom, la suite la commande. Arrêt au premier échec,
# avec le code de la commande.
etape() {
    nom=$1
    shift
    journal "$nom : début"
    if "$@"; then
        journal "$nom : OK"
    else
        code=$?
        journal "$nom : ÉCHEC (code $code)"
        exit "$code"
    fi
}

# Noms des variables absentes ou vides, jamais leurs valeurs.
exiger_variables() {
    manquantes=''
    for nom in "$@"; do
        eval "valeur=\${$nom:-}"
        [ -n "$valeur" ] || manquantes="$manquantes $nom"
    done
    [ -z "$manquantes" ] || refus "variables absentes ou vides :$manquantes"
}

depot_accessible() { restic cat config > /dev/null; }

psql_requete() { psql -X -q -At -v ON_ERROR_STOP=1 -U "$DB_USER" "$@"; }

sauvegarder() {
    approfondi=0
    case "${1:-}" in
        '') ;;
        --controle-approfondi) approfondi=1 ;;
        *) usage ;;
    esac
    exiger_variables RESTIC_REPOSITORY RESTIC_PASSWORD DB_NAME DB_USER PGPASSWORD
    # Jamais d'initialisation automatique : une adresse erronée ne crée pas
    # de dépôt ailleurs en silence.
    etape "dépôt" depot_accessible
    # Le dump passe de pg_dump à restic sans fichier en clair sur le disque.
    # Si pg_dump échoue, restic ne crée aucun instantané (jamais d'archive
    # tronquée). Format custom pour pg_restore, non compressé : restic
    # compresse et déduplique.
    etape "base" restic backup --host "$HOTE" --tag db --stdin-filename "$FICHIER_DUMP" \
        --stdin-from-command -- pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -Z 0
    # Médias après la base : leur copie contient tous les fichiers référencés
    # par le dump, sauf ceux supprimés entre les deux étapes (resoumission
    # d'un dossier KYC), que restaurer compte.
    etape "médias" restic backup --host "$HOTE" --tag media "$MEDIAS"
    # 7 quotidiennes, 4 hebdomadaires, 6 mensuelles, séparément pour la base
    # et les médias (regroupement par hôte et chemin).
    etape "rétention" restic forget --host "$HOTE" \
        --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
    if [ "$approfondi" = 1 ]; then
        part=$(( $(date -u +%s) / 604800 % PARTS_CONTROLE + 1 ))
        etape "contrôle approfondi (données relues : part $part/$PARTS_CONTROLE)" \
            restic check --read-data-subset="$part/$PARTS_CONTROLE"
    else
        etape "contrôle" restic check
    fi
}

restaurer_base() {
    restic dump --host "$HOTE" --tag db "$instantane_base" "/$FICHIER_DUMP" \
        | pg_restore -U "$DB_USER" -d "$cible" \
            --single-transaction --exit-on-error --no-owner --no-privileges
}

restaurer() {
    debut=$(date -u +%s)
    cible="anitche_restauration_$(date -u +%Y%m%d)"
    medias=/tmp/restauration/medias
    medias_temporaires=1
    instantane_base=latest
    instantane_medias=latest
    while [ $# -gt 0 ]; do
        [ $# -ge 2 ] || usage
        case "$1" in
            --base) cible=$2 ;;
            --medias) medias=$2; medias_temporaires=0 ;;
            --instantane-base) instantane_base=$2 ;;
            --instantane-medias) instantane_medias=$2 ;;
            *) usage ;;
        esac
        shift 2
    done
    exiger_variables RESTIC_REPOSITORY RESTIC_PASSWORD DB_NAME DB_USER PGPASSWORD

    # Cibles contrôlées avant toute écriture : jamais de données remplacées.
    case "$cible" in
        ''|[!a-z_]*|*[!a-z0-9_]*) refus "nom de base invalide (lettres minuscules, chiffres et _)." ;;
    esac
    [ "${#cible}" -le 63 ] || refus "nom de base trop long (63 caractères au plus)."
    if [ -e "$medias" ] && [ -n "$(ls -A "$medias")" ]; then
        refus "$medias n'est pas vide : les médias ne sont restaurés que dans un dossier vide."
    fi
    existe=$(printf "SELECT count(*) FROM pg_database WHERE datname = :'cible';\n" \
        | psql_requete -d postgres -v cible="$cible")
    if [ "$existe" = 1 ]; then
        objets=$(psql_requete -d "$cible" -c "SELECT count(*) FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
            AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            AND n.nspname NOT LIKE 'pg_toast%'")
        [ "$objets" = 0 ] || refus "la base $cible contient $objets tables, vues ou séquences : la restauration n'écrit que dans une base vide."
    fi

    etape "dépôt" depot_accessible
    if [ "$existe" = 1 ]; then
        journal "base $cible : existante et vide"
    else
        printf 'CREATE DATABASE :"cible";\n' | psql_requete -d postgres -v cible="$cible"
        journal "base $cible : créée"
    fi
    # Une seule transaction : en cas d'erreur, la base cible reste vide.
    etape "base (instantané $instantane_base)" restaurer_base
    # --verify relit chaque fichier restauré et le compare au dépôt.
    etape "médias (instantané $instantane_medias)" restic restore --host "$HOTE" --tag media \
        "$instantane_medias:$MEDIAS" --target "$medias" --verify

    echec=0
    extensions=$(psql_requete -d "$cible" \
        -c "SELECT count(*) FROM pg_extension WHERE extname IN ('unaccent', 'pg_trgm')")
    if [ "$extensions" = 2 ]; then
        journal "vérification extensions unaccent et pg_trgm : OK"
    else
        journal "vérification extensions unaccent et pg_trgm : ÉCHEC ($extensions sur 2)"
        echec=1
    fi

    # Nombres de lignes, avec ceux de la base vivante quand elle est
    # joignable : à titre indicatif, elle a pu changer depuis la sauvegarde.
    journal "nombres de lignes (base restaurée / base vivante $DB_NAME) :"
    for table in $TABLES_COMPTEES; do
        if ! restauree=$(psql_requete -d "$cible" -c "SELECT count(*) FROM $table"); then
            journal "  $table : ÉCHEC (table illisible)"
            echec=1
            continue
        fi
        vivante='-'
        if [ "$cible" != "$DB_NAME" ]; then
            vivante=$(psql_requete -d "$DB_NAME" -c "SELECT count(*) FROM $table" 2> /dev/null) || vivante='injoignable'
        fi
        journal "  $table : $restauree / $vivante"
    done

    references=$(mktemp)
    if psql_requete -d "$cible" -c "$REQUETE_FICHIERS" > "$references"; then
        total=0
        manquants=0
        while IFS= read -r chemin; do
            total=$((total + 1))
            if [ ! -f "$medias/$chemin" ]; then
                manquants=$((manquants + 1))
                [ "$manquants" -gt 20 ] || journal "  fichier manquant : $chemin"
            fi
        done < "$references"
        if [ "$manquants" = 0 ]; then
            journal "vérification fichiers référencés par la base : OK ($total présents)"
        else
            journal "vérification fichiers référencés par la base : ÉCHEC ($manquants manquants sur $total)"
            echec=1
        fi
    else
        journal "vérification fichiers référencés par la base : ÉCHEC (requête)"
        echec=1
    fi
    rm -f -- "$references"

    journal "durée totale de la restauration : $(( $(date -u +%s) - debut )) s"
    if [ "$medias_temporaires" = 1 ]; then
        journal "médias restaurés dans un dossier temporaire, supprimé avec le conteneur"
    fi
    if [ "$cible" != "$DB_NAME" ]; then
        journal "base $cible conservée pour inspection ; à supprimer ensuite : DROP DATABASE $cible;"
    fi
    if [ "$echec" = 1 ]; then
        journal "restauration : ÉCHEC d'au moins une vérification"
        exit 1
    fi
    journal "restauration : OK"
}

commande=${1:-}
[ $# -eq 0 ] || shift
case "$commande" in
    init)
        exiger_variables RESTIC_REPOSITORY RESTIC_PASSWORD
        restic init
        ;;
    sauvegarder) sauvegarder "$@" ;;
    instantanes)
        exiger_variables RESTIC_REPOSITORY RESTIC_PASSWORD
        restic snapshots --host "$HOTE"
        ;;
    restaurer) restaurer "$@" ;;
    *) usage ;;
esac
