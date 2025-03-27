import os
import tqdm
import pathlib
import networks.g2p

from lightning.pytorch.callbacks import Callback, TQDMProgressBar

from networks.utils.export_tool import Exporter
from networks.utils.post_processing import post_processing

from typing import Dict
from networks.utils import label
from networks.utils.metrics import Metric, VlabelerEditRatio, BoundaryEditRatio, BoundaryEditRatioWeighted
from evaluate import remove_ignored_phonemes

from networks.utils.get_melspec import MelSpecExtractor
from networks.utils.load_wav import load_wav


class StepProgressBar(TQDMProgressBar):
    def __init__(self):
        super().__init__()
        self._global_step = 0

    def on_train_batch_start(self, trainer, pl_module, batch, batch_idx):
        super().on_train_batch_start(trainer, pl_module, batch, batch_idx)
        self._global_step = trainer.global_step

    def get_metrics(self, trainer, pl_module):
        items = super().get_metrics(trainer, pl_module)
        items["step"] = self._global_step
        return items


class RecentCheckpointsCallback(Callback):
    def __init__(self, save_path, save_top_k=5, save_every_steps=5000):
        self.save_path = save_path
        self.save_top_k = save_top_k
        self.filename = "checkpoint-step={step}"
        self.saved_checkpoints = []
        self.save_every_steps = save_every_steps

    def on_train_batch_end(self, trainer, pl_module, outputs, batch, batch_idx):
        if trainer.global_step % self.save_every_steps == 0:
            checkpoint_path = os.path.join(
                self.save_path,
                self.filename.format(step=trainer.global_step) + ".ckpt"
            )
            trainer.save_checkpoint(checkpoint_path)
            self.saved_checkpoints.append(checkpoint_path)

            if len(self.saved_checkpoints) > self.save_top_k:
                oldest_checkpoint = self.saved_checkpoints.pop(0)
                if os.path.exists(oldest_checkpoint):
                    os.remove(oldest_checkpoint)


class VlabelerEvaluateCallback(Callback):
    def __init__(self, evaluate_folder, dictionary, out_tg_dir):
        super().__init__()
        self.evaluate_folder = pathlib.Path(evaluate_folder)
        self.out_tg_dir = pathlib.Path(out_tg_dir)
        self.grapheme_to_phoneme = networks.g2p.DictionaryG2P(**{"dictionary": dictionary})
        self.grapheme_to_phoneme.set_in_format('lab')
        self.dataset = self.grapheme_to_phoneme.get_dataset(pathlib.Path(evaluate_folder).rglob("*.wav"))
        self.get_melspec = None

    def on_validation_start(self, trainer, pl_module):
        if self.get_melspec is None:
            self.get_melspec = MelSpecExtractor(**trainer.model.melspec_config)

        predictions = []
        for batch in tqdm.tqdm(self.dataset, desc="evaluate_forward:"):
            wav_path, ph_seq, word_seq, ph_idx_to_word_idx = batch
            waveform = load_wav(
                wav_path, trainer.model.device, trainer.model.melspec_config["sample_rate"]
            )
            wav_length = waveform.shape[0] / trainer.model.melspec_config["sample_rate"]
            melspec = self.get_melspec(waveform).detach().unsqueeze(0)

            # load audio
            units = trainer.model.unitsEncoder.encode(waveform.unsqueeze(0),
                                                      trainer.model.melspec_config["sample_rate"],
                                                      trainer.model.melspec_config["hop_length"])
            input_feature = units.transpose(1, 2)

            (
                ph_seq,
                ph_intervals,
                word_seq,
                word_intervals,
                confidence,
                _,
                _,
            ) = trainer.model._infer_once(
                input_feature, melspec, wav_length, ph_seq, word_seq, ph_idx_to_word_idx, False, False
            )

            predictions.append((wav_path, wav_length, confidence, ph_seq, ph_intervals, word_seq, word_intervals,))

        predictions, log = post_processing(predictions)
        out_tg_dir = self.out_tg_dir / "evaluate" / str(trainer.global_step)
        exporter = Exporter(predictions, log, out_tg_dir)
        exporter.export(['textgrid'])

        iterable = out_tg_dir.rglob("*.TextGrid")

        metrics: Dict[str, Metric] = {
            "BoundaryEditRatio": BoundaryEditRatio(),
            "BoundaryEditRatioWeighted": BoundaryEditRatioWeighted(),
            "VlabelerEditRatio10-20ms": VlabelerEditRatio(move_min=0.01, move_max=0.02),
            "VlabelerEditRatio20-50ms": VlabelerEditRatio(move_min=0.02, move_max=0.05),
            "VlabelerEditRatio50-100ms": VlabelerEditRatio(move_min=0.05, move_max=0.1),
            "VlabelerEditRatio100-5000ms": VlabelerEditRatio(move_min=0.1, move_max=5.0)
        }

        for pred_file in tqdm.tqdm(iterable, desc="evaluate_compute:"):
            target_file = list(self.evaluate_folder.rglob(pathlib.Path(pred_file).name))
            if not target_file:
                continue
            target_file = target_file[0]

            pred_tier = label.textgrid_from_file(pred_file)[-1]
            target_tier = label.textgrid_from_file(target_file)[-1]
            pred_tier = remove_ignored_phonemes("", pred_tier)
            target_tier = remove_ignored_phonemes("", target_tier)

            for metric in metrics.values():
                metric.update(pred_tier, target_tier)

        result = {key: metric.compute() for key, metric in metrics.items()}

        vlabeler_loss = result["VlabelerEditRatio10-20ms"] * 0.1 + result["VlabelerEditRatio20-50ms"] * 0.2 + \
                        result["VlabelerEditRatio50-100ms"] * 0.3 + result["VlabelerEditRatio100-5000ms"] * 0.4
        result["vlabeler_loss"] = vlabeler_loss
        result["total"] = vlabeler_loss * 0.5 + result["BoundaryEditRatioWeighted"] * 0.5

        if trainer.logger:
            for metric_name, metric_value in result.items():
                trainer.model.log(f"evaluate/{metric_name}", metric_value)
