from django.db import models, transaction

from django.template.defaultfilters import slugify
from django.urls import reverse

import uuid

import numpy as np
import cv2 as cv
from imgproc.wound import wound_contours, free_cells

import tablib


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=1024)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("project-list") + f"#project-{self.pk}"

    def report(self, fmt="csv"):
        data = tablib.Dataset()

        experiments = self.experiments.all()
        names = [e.name for e in experiments]
        reports = [e.report(None) for e in experiments]

        # The frame column from the longest experiment
        data.append_col(max(reports, key=len).get_col(0))

        # A column for each experiment
        for rep in reports:
            col = rep.get_col(1)
            col += [None] * (len(data) - len(col)) # Pad to correct length
            data.append_col(col)

        data.headers = ["Frame"] + names

        if fmt is None:
            return data
        return data.export(fmt)


class Experiment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    project = models.ForeignKey(Project, related_name="experiments", on_delete=models.CASCADE)
    name = models.CharField(max_length=1024)

    def __str__(self):
        return f"{self.project.name}_{self.name}"

    def get_absolute_url(self):
        return reverse("experiment-detail", kwargs={"project": self.project.pk, "pk": self.pk})

    def report(self, fmt="csv"):
        data = tablib.Dataset(headers=["Frame", "Wound area %"])
        for i, frame in enumerate(self.frames.all()):
            data.append((i, frame.surface))

        if fmt is None:
            return data
        return data.export(fmt)


class Frame(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    def upload_to(instance, filename):
        return f"{instance.experiment.project.name}/{instance.experiment.name}/{filename}"

    def __str__(self):
        return f"{self.experiment}_{self.number:03}"

    experiment = models.ForeignKey(Experiment, related_name="frames", on_delete=models.CASCADE)
    number = models.PositiveIntegerField()
    image = models.ImageField(upload_to=upload_to)

    class Meta:
        unique_together = [("experiment", "number")]
        ordering = ["experiment", "number"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._img = None

    @property
    def raw_img(self):
        if self._img is None:
            with self.image.open() as f:
                self._img = cv.imdecode(np.frombuffer(f.read(), np.uint8), cv.IMREAD_GRAYSCALE)
        #if len(img.shape) > 2:
        #    img = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
        return self._img

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("frame-view", kwargs={"pk": self.pk})

    def img(self, equalized=False, lut_in=None, lut_out=None, mask=False):
        if mask:
            return self.mask()
        
        res = self.raw_img

        if equalized:
            res = cv.equalizeHist(res)

        if lut_in is not None and lut_out is not None:
            lut_8u = np.interp(np.arange(0, 256), lut_in, lut_out).astype(np.uint8)
            res = cv.LUT(res, lut_8u)

        return res

    def mask(self):
        mask = np.zeros((self.image.height, self.image.width), dtype=np.uint8)
        for poly in self.polygons.all():
            if poly.operation == "+":
                mask |= poly.mask() == 255
            elif poly.operation == "-":
                mask &= poly.mask() != 255
        mask *= 255
        return mask

    def img_and_histogram(self, **kwargs):
        image = self.img(**kwargs)
        hist = np.bincount(image.ravel(), minlength=256)
        return image, hist

    def histogram(self, **kwargs):
        return self.img_and_histogram(**kwargs)[1]

    def img_url(self, **kwargs):
        import urllib.parse

        return self.get_absolute_url() + ("?" + urllib.parse.urlencode(kwargs) if kwargs else "")

    def detect(self):
        img = self.img()

        contours = wound_contours(img, approx=2)

        with transaction.atomic():
            res = []
            for contour in contours:
                contour = contour.astype(np.float64)
                contour[:, 0] /= img.shape[0]
                contour[:, 1] /= img.shape[1]

                res.append(Polygon.objects.create(frame=self, data=contour.tolist()))
            return res

    def detect_free_cells(self):
        img = self.img()

        wound = wound_contours(img, approx=2)
        cells = free_cells(img, wound, approx=2)

        with transaction.atomic():
            res = []
            for contour in cells:
                contour = contour.astype(np.float64)
                contour[:, 0] /= img.shape[0]
                contour[:, 1] /= img.shape[1]

                res.append(Polygon.objects.create(frame=self, data=contour.tolist(), operation="-"))
            return res

    def detect_full(self):
        img = self.img()

        wound = wound_contours(img, approx=2)
        cells = free_cells(img, wound, approx=2)

        with transaction.atomic():
            res = []

            for contour in wound:
                contour = contour.astype(np.float64)
                contour[:, 0] /= img.shape[0]
                contour[:, 1] /= img.shape[1]
                res.append(Polygon.objects.create(frame=self, data=contour.tolist()))

            for contour in cells:
                contour = contour.astype(np.float64)
                contour[:, 0] /= img.shape[0]
                contour[:, 1] /= img.shape[1]

                res.append(Polygon.objects.create(frame=self, data=contour.tolist(), operation="-"))
            return res

    @property
    def surface(self):
        return sum(poly.surface for poly in self.polygons.all())


class Polygon(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    frame = models.ForeignKey(Frame, related_name="polygons", on_delete=models.CASCADE)
    data = models.JSONField()

    operation = models.CharField(
        max_length=1,
        choices=[("+", "Add"), ("-", "Subtract")],
        default="+",
    )

    def mask(self):
        mask = np.zeros((self.frame.image.height, self.frame.image.width), np.uint8)
        pts = np.array([[int(pt[0]*self.frame.image.height), int(pt[1]*self.frame.image.width)] for pt in self.data])
        cv.fillPoly(mask, [pts], 255)
        return mask

    @property
    def surface(self):
        points = np.array(self.data, dtype=np.float32)
        points[:, 0] *= self.frame.image.width
        points[:, 1] *= self.frame.image.height

        sign = -1 if self.operation == "-" else 1
        return (sign * cv.contourArea(points)) / (self.frame.image.width * self.frame.image.height)
