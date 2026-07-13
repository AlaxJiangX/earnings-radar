import hashlib
import json

from django.db import migrations


def _canonical_normalized_value(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _evidence_key(
    *,
    raw_data_record_id: object,
    target_type: str,
    target_id: object,
    field_name: str,
    normalized_value: object,
    normalizer_version: str,
) -> str:
    identity = {
        "field_name": field_name,
        "normalized_value": _canonical_normalized_value(normalized_value),
        "normalizer_version": normalizer_version,
        "raw_data_record_id": str(raw_data_record_id),
        "target_id": str(target_id),
        "target_type": target_type,
    }
    serialized = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()


def rekey_source_evidence(apps, schema_editor) -> None:
    SourceEvidence = apps.get_model("audit", "SourceEvidence")
    evidence_rows = SourceEvidence.objects.order_by("pk").all()
    pending_updates = []

    for evidence in evidence_rows.iterator(chunk_size=1000):
        evidence.evidence_key = _evidence_key(
            raw_data_record_id=evidence.raw_data_record_id,
            target_type=evidence.target_type,
            target_id=evidence.target_id,
            field_name=evidence.field_name,
            normalized_value=evidence.normalized_value,
            normalizer_version=evidence.normalizer_version,
        )
        pending_updates.append(evidence)
        if len(pending_updates) == 1000:
            SourceEvidence.objects.bulk_update(pending_updates, ("evidence_key",))
            pending_updates.clear()

    if pending_updates:
        SourceEvidence.objects.bulk_update(pending_updates, ("evidence_key",))


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0003_source_evidence"),
    ]

    operations = [
        migrations.RunPython(
            rekey_source_evidence,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
