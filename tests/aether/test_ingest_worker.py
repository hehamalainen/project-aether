from sqlmodel import SQLModel, Session, create_engine

from apps.aether_server.ingest_worker import _list_ingest_active_repo_ids
from packages.aether.models.entities import Proposal, ProposalStatus, Repository


def test_list_ingest_active_repo_ids_only_returns_ingested(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'aether.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        ingested_repo = Repository(id="repo_ingested", name="ingested")
        pending_repo = Repository(id="repo_pending", name="pending")
        session.add(ingested_repo)
        session.add(pending_repo)
        session.add(
            Proposal(
                id="prop_ingested",
                repo_id=ingested_repo.id,
                agent_id="agent",
                change_content="diff",
                affected_paths='["a.py"]',
                dependencies="[]",
                status=ProposalStatus.INGESTED,
            )
        )
        session.add(
            Proposal(
                id="prop_pending",
                repo_id=pending_repo.id,
                agent_id="agent",
                change_content="diff",
                affected_paths='["b.py"]',
                dependencies="[]",
                status=ProposalStatus.PENDING,
            )
        )
        session.commit()

        repo_ids = _list_ingest_active_repo_ids(session)

    assert repo_ids == ["repo_ingested"]
