from __future__ import annotations

import hashlib
import io
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from PIL import Image, ImageOps

from comparebot.application.ports.image_ranker import ImageScore, RankableImage

_DEFAULT_REVISION = "ed25f3a31f01632728cabb09d1542f84ab7b0056"


class DinoV2Ranker:
    def __init__(
        self,
        *,
        model_name: str = "facebook/dinov2-small",
        model_revision: str = _DEFAULT_REVISION,
        device: str | None = None,
        batch_size: int = 16,
        cache_size: int = 4096,
    ) -> None:
        if batch_size < 1 or cache_size < 1:
            raise ValueError("batch_size and cache_size must be positive")
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModel
        except ImportError as error:
            raise RuntimeError("install comparebot[dinov2]") from error

        self._torch = torch
        self._device = device or self._best_device(torch)
        self._model_name = model_name
        self._model_revision = model_revision
        self._batch_size = batch_size
        self._cache_size = cache_size
        self._embedding_cache: OrderedDict[str, Any] = OrderedDict()
        self._processor = AutoImageProcessor.from_pretrained(
            model_name, revision=model_revision, use_fast=False
        )
        self._model = (
            AutoModel.from_pretrained(model_name, revision=model_revision)
            .eval()
            .to(self._device)
        )

    @staticmethod
    def _best_device(torch) -> str:
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    @property
    def model_version(self) -> str:
        return f"{self._model_name}@{self._model_revision[:12]}"

    @property
    def device(self) -> str:
        return self._device

    def rank(
        self,
        reference: bytes,
        candidates: Sequence[RankableImage],
    ) -> tuple[ImageScore, ...]:
        if not candidates:
            return ()
        reference_vector = self._encode([reference])[0]
        candidate_vectors = []
        for offset in range(0, len(candidates), self._batch_size):
            batch = candidates[offset : offset + self._batch_size]
            candidate_vectors.append(self._encode([item.content for item in batch]))
        matrix = self._torch.cat(candidate_vectors, dim=0)
        similarities = (matrix @ reference_vector).detach().cpu().tolist()
        ordered = sorted(
            zip(candidates, similarities, strict=True),
            key=lambda item: (-item[1], item[0].candidate_id),
        )
        return tuple(
            ImageScore(candidate.candidate_id, round(float(score), 6), rank)
            for rank, (candidate, score) in enumerate(ordered, start=1)
        )

    def _encode(self, images: Sequence[bytes]):
        keys = [hashlib.sha256(content).hexdigest() for content in images]
        missing = {
            key: content
            for key, content in zip(keys, images, strict=True)
            if key not in self._embedding_cache
        }
        missing_items = list(missing.items())
        for offset in range(0, len(missing_items), self._batch_size):
            batch = missing_items[offset : offset + self._batch_size]
            decoded = [self._decode(content) for _, content in batch]
            inputs = self._processor(images=decoded, return_tensors="pt")
            inputs = {key: value.to(self._device) for key, value in inputs.items()}
            with self._torch.inference_mode():
                output = self._model(**inputs)
                vectors = self._torch.nn.functional.normalize(
                    output.last_hidden_state[:, 0], p=2, dim=1
                )
            for (key, _), vector in zip(batch, vectors, strict=True):
                self._embedding_cache[key] = vector.detach().cpu()
                self._embedding_cache.move_to_end(key)
                if len(self._embedding_cache) > self._cache_size:
                    self._embedding_cache.popitem(last=False)
        for key in keys:
            self._embedding_cache.move_to_end(key)
        return self._torch.stack([self._embedding_cache[key] for key in keys]).to(self._device)

    @staticmethod
    def _decode(content: bytes) -> Image.Image:
        try:
            with Image.open(io.BytesIO(content)) as source:
                return ImageOps.exif_transpose(source).convert("RGB")
        except (OSError, ValueError) as error:
            raise ValueError("unsupported image content") from error
