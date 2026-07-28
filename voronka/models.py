"""Схемы входящих payload'ов."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FormLead(BaseModel):
    """Заявка с лендинга или произвольного вебхука формы."""

    name: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=50)
    email: str = Field(default="", max_length=200)
    telegram: str = Field(default="", max_length=100)
    comment: str = Field(default="", max_length=2000)
    form_id: str = Field(default="landing", max_length=64)
    utm_source: str = Field(default="", max_length=100)
    # Клиент может прислать свой идентификатор доставки; на дедуп он не влияет,
    # но попадает в журнал и помогает разбирать инциденты.
    request_id: str = Field(default="", max_length=100)


class BotHelpResult(BaseModel):
    """Итог диалога в BotHelp, прилетающий действием «Внешний запрос».

    BotHelp подставляет в тело значения пользовательских полей через макросы.
    Телефон/email нужны, чтобы связать диалог с уже созданной сделкой: ключ
    считается по тем же правилам, что и для формы.
    """

    phone: str = Field(default="", max_length=50)
    email: str = Field(default="", max_length=200)
    telegram_id: str = Field(default="", max_length=100)
    form_id: str = Field(default="landing", max_length=64)

    qualified: bool = False
    budget: str = Field(default="", max_length=100)
    timeline: str = Field(default="", max_length=100)
    segment: str = Field(default="", max_length=100)
    transcript: str = Field(default="", max_length=4000)
    # Идентификатор шага/сценария — чтобы повторная отправка того же шага
    # не плодила примечания в сделке.
    step_id: str = Field(default="result", max_length=64)


class WebhookAck(BaseModel):
    status: Literal["accepted", "duplicate", "rejected"]
    trace_id: str
    dedup_key: str | None = None
    detail: str | None = None
