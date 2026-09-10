from comparebot.application.ports.image_ranker import ImageScore
from comparebot.application.services.search_and_rank import SearchAndRankService
from comparebot.domain.models import ProductQuery, SearchCandidate


class SearchStub:
    async def search_by_image(self, image: bytes):
        assert image == b"reference"
        duplicate = SearchCandidate("a", "A duplicate", "https://img/a2", "url-a", 3)
        return (
            SearchCandidate("a", "A", "https://img/a", "url-a", 1),
            SearchCandidate("b", "B", "https://img/b", "url-b", 2),
            duplicate,
        )


class ImageStub:
    async def load(self, url: str) -> bytes:
        values = {
            "https://ozon/query": b"reference",
            "https://img/a2": b"image-a",
            "https://img/b": b"image-b",
        }
        return values[url]


class RankerStub:
    model_version = "fake-dinov2"
    device = "cpu"

    def rank(self, reference, candidates):
        assert reference == b"reference"
        assert {item.candidate_id for item in candidates} == {"a", "b"}
        return (ImageScore("b", 0.9, 1), ImageScore("a", 0.7, 2))


async def test_service_deduplicates_downloads_and_merges_ranked_results() -> None:
    result = await SearchAndRankService(SearchStub(), ImageStub(), RankerStub()).run(
        ProductQuery("ozon-1", "query", "https://ozon/query"),
        top_k=1,
    )

    assert result.raw_candidate_count == 3
    assert result.ranked_candidate_count == 2
    assert result.image_download_failures == 0
    assert result.candidates[0].candidate.offer_id == "b"
    assert result.candidates[0].dinov2_similarity == 0.9
    assert result.model_version == "fake-dinov2"

