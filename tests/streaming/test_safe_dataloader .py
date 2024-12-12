import os
import unittest

import pytest
import torch
from litdata.constants import _VIZ_TRACKER_AVAILABLE
from litdata.streaming import Cache, CombinedStreamingDataset, StreamingDataset #, StreamingDataLoader
from litdata.streaming import dataloader as streaming_dataloader_module
from litdata.streaming.dataloader import SafeStreamingDataLoader as StreamingDataLoader
from torch import tensor


class TestStatefulDataset:
    def __init__(self, size, step):
        self.size = size
        self.step = step
        self.counter = 0
        self.shuffle = None
        self.drop_last = None

    def set_shuffle(self, shuffle):
        self.shuffle = shuffle

    def __len__(self):
        return self.size

    def __iter__(self):
        self.counter = 0
        return self

    def __next__(self):
        if self.counter == self.size:
            raise StopIteration
        value = self.step * self.counter
        self.counter += 1
        return value

    def state_dict(self, *args, **kwargs):
        return {"counter": self.counter}

    def load_state_dict(self, state_dict):
        self.counter = state_dict["counter"]

    def set_epoch(self, current_epoch):
        pass

    def set_drop_last(self, drop_last):
        self.drop_last = drop_last

    def set_batch_size(self, batch_size):
        self.batch_size = batch_size

    def set_num_workers(self, num_workers):
        self.num_workers = num_workers


class TestCombinedStreamingDataset(CombinedStreamingDataset):
    def _check_datasets(self, datasets) -> None:
        pass

    def reset_state_dict(self):
        pass


def test_streaming_dataloader():
    dataset = TestCombinedStreamingDataset(
        [TestStatefulDataset(10, 1), TestStatefulDataset(10, -1)],
        42,
        weights=(0.5, 0.5),
        iterate_over_all=False,
    )
    dataloader = StreamingDataLoader(dataset, batch_size=2)
    dataloader_iter = iter(dataloader)
    batches = []
    for batch in dataloader_iter:
        batches.append(batch)

    expected = [
        tensor([0, 0]),
        tensor([1, 2]),
        tensor([-1, -2]),
        tensor([-3, 3]),
        tensor([4, 5]),
        tensor([6, -4]),
        tensor([7, 8]),
        tensor([-5, -6]),
        tensor([9, -7]),
        tensor([-8]),
    ]

    for exp, gen in zip(expected, batches):
        assert torch.equal(exp, gen)

    assert dataloader.state_dict() == {
        "dataset": {"0": {"counter": 10}, "1": {"counter": 9}},
        "current_epoch": 0,
        "latest_worker_idx": 0,
        "num_samples_yielded": {0: [10, 9]},
    }


@pytest.mark.skip(reason="Profiling patches torch which leads to undesired test interactions")
@pytest.mark.skipif(not _VIZ_TRACKER_AVAILABLE, reason="viz tracker required")
@pytest.mark.parametrize("profile", [2, True])
def test_dataloader_profiling(profile, tmpdir, monkeypatch):
    monkeypatch.setattr(streaming_dataloader_module, "_VIZ_TRACKER_AVAILABLE", True)

    dataset = TestCombinedStreamingDataset(
        [TestStatefulDataset(10, 1), TestStatefulDataset(10, -1)],
        42,
        weights=(0.5, 0.5),
        iterate_over_all=False,
    )
    dataloader = StreamingDataLoader(
        dataset, batch_size=2, profile_batches=profile, profile_dir=str(tmpdir), num_workers=1
    )
    dataloader_iter = iter(dataloader)
    batches = []
    for batch in dataloader_iter:
        batches.append(batch)

    assert os.path.exists(os.path.join(tmpdir, "result.json"))


def test_dataloader_shuffle():
    dataset = TestCombinedStreamingDataset(
        [TestStatefulDataset(10, 1), TestStatefulDataset(10, -1)], 42, weights=(0.5, 0.5), iterate_over_all=False
    )
    assert dataset._datasets[0].shuffle is None
    assert dataset._datasets[1].shuffle is None
    StreamingDataLoader(dataset, batch_size=2, num_workers=1, shuffle=True)
    assert dataset._datasets[0].shuffle
    assert dataset._datasets[1].shuffle


class TestStatefulDatasetDict(TestStatefulDataset):
    def __next__(self):
        return {"value": super().__next__()}


def custom_collate_fn(samples):
    assert len(samples) == 2
    assert "value" in samples[0]
    return "received"


def test_custom_collate():
    dataset = TestCombinedStreamingDataset(
        [TestStatefulDatasetDict(10, 1), TestStatefulDatasetDict(10, -1)],
        42,
        weights=(0.5, 0.5),
        iterate_over_all=False,
    )
    assert dataset._datasets[0].shuffle is None
    assert dataset._datasets[1].shuffle is None
    dataloader = StreamingDataLoader(dataset, batch_size=2, num_workers=0, shuffle=True, collate_fn=custom_collate_fn)
    assert dataset._datasets[0].shuffle
    assert dataset._datasets[1].shuffle
    dataloader_iter = iter(dataloader)
    assert next(dataloader_iter) == "received"
    assert dataloader._num_samples_yielded_combined[0] == [dataset._datasets[0].counter, dataset._datasets[1].counter]


def test_custom_collate_multiworker():
    dataset = TestCombinedStreamingDataset(
        [TestStatefulDatasetDict(10, 1), TestStatefulDatasetDict(10, -1)],
        42,
        weights=(0.5, 0.5),
        iterate_over_all=False,
    )
    assert dataset._datasets[0].shuffle is None
    assert dataset._datasets[1].shuffle is None
    dataloader = StreamingDataLoader(dataset, batch_size=2, num_workers=3, shuffle=True, collate_fn=custom_collate_fn)
    assert dataset._datasets[0].shuffle
    assert dataset._datasets[1].shuffle
    dataloader_iter = iter(dataloader)
    assert next(dataloader_iter) == "received"
    assert dataloader._num_samples_yielded_combined[0] == [1, 1]
    assert next(dataloader_iter) == "received"
    assert dataloader._num_samples_yielded_combined[1] == [1, 1]
    assert next(dataloader_iter) == "received"
    assert dataloader._num_samples_yielded_combined[2] == [1, 1]
    assert next(dataloader_iter) == "received"
    assert dataloader._num_samples_yielded_combined[0] == [3, 1]

    # Iterate through the remaining samples
    try:
        while next(dataloader_iter) == "received":
            continue
    except AssertionError:
        assert dataloader._num_samples_yielded_combined == {0: [10, 8], 1: [10, 8], 2: [10, 8]}

    # Try calling the state_dict. No error should follow
    _state_dict = dataloader.state_dict()


def test_dataloader_no_workers(tmpdir):
    cache = Cache(input_dir=str(tmpdir), chunk_bytes="64MB")
    for i in range(1000):
        cache[i] = i

    cache.done()
    cache.merge()

    dataset = StreamingDataset(str(tmpdir), shuffle=True)
    dataloader = StreamingDataLoader(dataset)
    assert len(dataset) == 1000
    assert len(dataloader) == 1000
    assert len(dataset) == 1000


@pytest.mark.timeout(120)
def test_dataloader_with_loading_states(tmpdir):
    cache = Cache(input_dir=str(tmpdir), chunk_bytes="64MB")
    for i in range(100):
        cache[i] = i
    cache.done()
    cache.merge()

    dataset = StreamingDataset(str(tmpdir), shuffle=True)

    # Test dataloader without explicit num workers
    dataloader = StreamingDataLoader(dataset, batch_size=4)
    dataloader.load_state_dict(dataloader.state_dict())
    batch = next(iter(dataloader))
    assert len(batch) == 4, "Batch size should be 4"
    assert len(dataloader) == 25, "Dataloader length should be 25 (100 items / batch size 4)"

    # Test dataloader with num workers
    dataloader = StreamingDataLoader(dataset, batch_size=4, num_workers=2)
    assert len(dataloader) == 25, "Dataloader length should be 25 (100 items / batch size 4)"

    # Verify dataloader state after partial iteration
    for batch_idx, batch in enumerate(dataloader):
        assert dataloader.current_epoch == 1, "Current epoch should be 1"
        if batch_idx == 10:
            break
    dataloader.load_state_dict(dataloader.state_dict())
    assert dataloader.restore
    # Verify remaining batches in the first epoch
    count = 0
    for _ in dataloader:
        assert dataloader.current_epoch == 1, "Current epoch should be 1"
        count += 1
    assert count == 15, "There should be at least 15 batches remaining in the first epoch"
    assert not dataloader.restore

    # Verify batches in the second epoch
    count = 0
    for _ in dataloader:
        assert dataloader.current_epoch == 2, "Current epoch should be 2"
        count += 1
    assert count >= 25, "There should be at least 25 batches in the second epoch"

    # Verify that the datalaoder can resume after complete last epoch
    dataloader.load_state_dict(dataloader.state_dict())
    assert not dataloader.restore
    count = 0
    for _ in dataloader:
        assert dataloader.current_epoch == 3, "Current epoch should be 3"
        count += 1
    assert count >= 25, "There should be at least 25 batches in the third epoch"


@pytest.mark.timeout(120)
def test_dataloader_states_with_persistent_workers(tmpdir):
    cache = Cache(input_dir=str(tmpdir), chunk_bytes="64MB")
    for i in range(100):
        cache[i] = i
    cache.done()
    cache.merge()

    dataset = StreamingDataset(str(tmpdir), shuffle=True)

    dataloader = StreamingDataLoader(dataset, batch_size=4, num_workers=2)
    assert len(dataloader) == 25, "Dataloader length should be 25 (100 items / batch size 4)"

    # Verify dataloader state after partial iteration
    for batch_idx, batch in enumerate(dataloader):
        assert dataloader.current_epoch == 1, "Current epoch should be 1"
        if batch_idx == 10:
            break

    prev_dataloader_state = dataloader.state_dict()
    dataloader = StreamingDataLoader(dataset, batch_size=4, num_workers=2, persistent_workers=True)
    dataloader.load_state_dict(prev_dataloader_state)
    assert dataloader.restore

    # Verify remaining batches in the first epoch
    count = 0
    for _ in dataloader:
        assert dataloader.current_epoch == 1, "Current epoch should be 1"
        count += 1
    assert count == 15, "There should be at least 15 batches remaining in the first epoch"
    assert not dataloader.restore

    # Verify batches in the second epoch
    count = 0
    for _ in dataloader:
        assert dataloader.current_epoch == 2, "Current epoch should be 2"
        count += 1
    assert count >= 25, "There should be at least 25 batches in the second epoch"

    # Verify that the datalaoder can resume after complete last epoch
    dataloader.load_state_dict(dataloader.state_dict())
    assert not dataloader.restore
    count = 0
    for _ in dataloader:
        assert dataloader.current_epoch == 3, "Current epoch should be 3"
        count += 1
    assert count >= 25, "There should be at least 25 batches in the third epoch"


@pytest.mark.timeout(60)
def test_resume_dataloader_with_new_dataset(tmpdir):
    dataset_1_path = tmpdir.join("dataset_1")
    dataset_2_path = tmpdir.join("dataset_2")
    for dataset in [dataset_1_path, dataset_2_path]:
        cache = Cache(input_dir=str(dataset), chunk_bytes="64MB")
        for i in range(50):
            cache[i] = i
        cache.done()
        cache.merge()
    dataset = StreamingDataset(str(dataset_1_path), shuffle=True)
    dataloader = StreamingDataLoader(dataset, batch_size=4, num_workers=2)
    for _ in dataloader:
        assert dataloader.current_epoch == 1, "Current epoch should be 1"

    dataloader_state = dataloader.state_dict()
    dataset = StreamingDataset(str(dataset_2_path), shuffle=True)
    dataloader = StreamingDataLoader(dataset, batch_size=4, num_workers=2)
    dataloader.load_state_dict(dataloader_state)
    for _ in dataloader:
        assert dataloader.current_epoch == 2, "Current epoch should be 2"
