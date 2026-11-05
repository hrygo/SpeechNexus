"""Public catalogue consumes the canonical speech HTTP surface."""

from speechnexus_contracts.profile_public_http import validate_response


def test_public_list_accepts_published_revision_without_an_active_default() -> None:
    validate_response(
        "listProfiles",
        200,
        {
            "body": {
                "object": "nexus.list",
                "data": [
                    {
                        "id": "synthetic-profile",
                        "revision": "1",
                        "current_revision": None,
                        "display_name": "Synthetic",
                        "kind": "asr",
                        "release_tier": "experimental",
                        "execution_classes": ["batch"],
                    }
                ],
                "has_more": False,
                "next_cursor": None,
            }
        },
    )
